"""
Briefing Diário - Mercado Esportivo
Endpoint HTTP que gera o briefing, salva no Supabase, dispara no Telegram e posta no Twitter.
"""

import os, json, io, re, traceback
from datetime import datetime, timezone, timedelta, date
from flask import Flask, request, jsonify
import requests
import anthropic
from PIL import Image, ImageDraw, ImageFont
import tweepy

# ============================================================
# CREDENCIAIS
# ============================================================
SUPABASE_URL     = "https://yfdrifvhsiumdxgypkjm.supabase.co"
SUPABASE_KEY     = os.environ.get("SUPABASE_KEY", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-4659428992")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_KEY", "")
TW_API_KEY       = os.environ.get("TW_API_KEY", "")
TW_API_SECRET    = os.environ.get("TW_API_SECRET", "")
TW_ACCESS_TOKEN  = os.environ.get("TW_ACCESS_TOKEN", "")
TW_ACCESS_SECRET = os.environ.get("TW_ACCESS_SECRET", "")
DASH_URL         = "https://sreis27.github.io/mercado-esportivo-planilha"
INICIO_OPERACAO  = "2025-06-01"

BRT = timezone(timedelta(hours=-3))

app = Flask(__name__)

# ============================================================
# HELPERS
# ============================================================
def sb_headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=representation'
    }

def sb_get(path):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb_headers(), timeout=30)
    r.raise_for_status()
    return r.json()

def sb_upsert(table, body, conflict='data_ref'):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={conflict}",
        headers={**sb_headers(), 'Prefer': 'resolution=merge-duplicates,return=representation'},
        json=body, timeout=30
    )
    if not r.ok:
        print(f"sb_upsert erro: {r.status_code} {r.text}")
    r.raise_for_status()
    return r.json()

def fmtU(v):
    sign = '+' if v >= 0 else ''
    return f"{sign}{v:.1f}u".replace('.', ',')

def fmtR(v):
    return f"R$ {v:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')

def pct(v):
    sign = '+' if v >= 0 else ''
    return f"{sign}{v:.1f}%".replace('.', ',')

# ============================================================
# BUSCAR E AGREGAR DADOS
# ============================================================
def get_stake_valor(tipster_id, data_evento, stakes):
    if not tipster_id or not data_evento:
        return None
    candidatas = [s for s in stakes if s['tipster_id'] == tipster_id and s['vigente_a_partir'] <= data_evento]
    if not candidatas:
        return None
    candidatas.sort(key=lambda s: s['vigente_a_partir'], reverse=True)
    return float(candidatas[0]['valor_reais'])

def agregar_periodo(apostas, stakes, de, ate):
    """Agrega apostas de um período [de, ate] (inclusive)."""
    filtradas = [a for a in apostas if de <= (a.get('data_evento') or '') <= ate]
    settled = [a for a in filtradas if a.get('status') not in ('PENDING',)]
    won     = [a for a in filtradas if a.get('status') in ('WON', 'HALF WON')]

    plU = sum(float(a.get('lucro_unidades') or 0) for a in settled)
    invU = sum(float(a.get('stake_unidades') or 0) for a in filtradas)

    invR = 0.0
    plR  = 0.0
    for a in filtradas:
        su = float(a.get('stake_unidades') or 0)
        lu = float(a.get('lucro_unidades') or 0)
        sv = get_stake_valor(a.get('tipster_id'), a.get('data_evento'), stakes) or 1
        invR += su * sv
        if a.get('status') not in ('PENDING',):
            plR += lu * sv

    roiU   = (plU / invU * 100) if invU > 0 else 0
    roiR   = (plR / invR * 100) if invR > 0 else 0
    acerto = (len(won) / len(settled) * 100) if settled else 0

    return {
        'entradas': len(filtradas),
        'settled': len(settled),
        'won': len(won),
        'plU': plU, 'plR': plR,
        'invU': invU, 'invR': invR,
        'roiU': roiU, 'roiR': roiR,
        'acerto': acerto,
    }

def tops_periodo(apostas, stakes, de, ate, cache_tipsters, cache_bookies, cache_operadores):
    filtradas = [a for a in apostas if de <= (a.get('data_evento') or '') <= ate and a.get('status') != 'PENDING']

    def agrupar(field, cache_arr):
        mp = {}
        for a in filtradas:
            fid = a.get(field)
            item = next((c for c in cache_arr if c['id'] == fid), None) if cache_arr else None
            nome = item['nome'] if item else 'Outros'
            mp[nome] = mp.get(nome, 0) + float(a.get('lucro_unidades') or 0)
        return sorted(mp.items(), key=lambda x: x[1], reverse=True)

    top_t = agrupar('tipster_id', cache_tipsters)
    top_b = agrupar('bookie_id', cache_bookies)
    top_o = agrupar('operador_id', cache_operadores)

    return {
        'tipsters': top_t,
        'bookies': top_b,
        'operadores': top_o,
    }

# ============================================================
# GERAR CONTEÚDO VIA CLAUDE
# ============================================================
def gerar_conteudo_claude(data_ref, dia, mes, acum, tops_dia, tops_mes):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    dados_resumidos = f"""
DATA: {data_ref}

FECHAMENTO DO DIA:
- Entradas: {dia['entradas']}
- P/L: {fmtU(dia['plU'])} ({fmtR(dia['plR'])})
- ROI: {pct(dia['roiU'])} (u) / {pct(dia['roiR'])} (R$)
- Taxa de acerto: {dia['acerto']:.1f}% ({dia['won']}/{dia['settled']})
- Investimento: {fmtU(dia['invU'])} ({fmtR(dia['invR'])})

MÊS:
- Entradas: {mes['entradas']}
- P/L: {fmtU(mes['plU'])} ({fmtR(mes['plR'])})
- ROI: {pct(mes['roiU'])}

ACUMULADO (desde 01/06/2025):
- Entradas: {acum['entradas']}
- P/L: {fmtU(acum['plU'])}
- ROI: {pct(acum['roiU'])}

TOP TIPSTERS DO DIA (nome, p/l em unidades):
{json.dumps(tops_dia['tipsters'][:5], ensure_ascii=False)}

TOP BOOKIES DO DIA:
{json.dumps(tops_dia['bookies'][:5], ensure_ascii=False)}

TOP OPERADORES DO DIA:
{json.dumps(tops_dia['operadores'][:5], ensure_ascii=False)}
"""

    prompt = f"""Você é o gerador de briefings diários do Mercado Esportivo, uma operação profissional de apostas esportivas baseada em EV+ e volume.

Dados de hoje:
{dados_resumidos}

Gere um JSON (sem markdown, sem ```, só o JSON puro) com:

1. "frase_twitter" — UMA frase curta (máx 80 caracteres) criativa e sem clichê pra ilustrar o card do Twitter. EVITE frases manjadas tipo "o método fala mais alto", "consistência é tudo", "no longo prazo". Seja original, direto, meio filosófico, meio técnico. Tom de operador profissional que sabe o que faz.

2. "destaque_positivo" — objeto com "titulo" (máx 60 chars) e "texto" (1-2 frases, máx 180 chars). Elege o tipster/bookie/operador de destaque positivo do dia.

3. "destaque_alerta" — objeto com "titulo" e "texto". Elege um alerta relevante (sequência ruim, bookie no vermelho, etc). Se não houver nada preocupante, retorne null.

4. "curiosidade" — objeto com "titulo" e "texto". Alguma curiosidade ou recorde do dia (maior odd, entrada inusitada, primeira vez, etc). Pode ser null se não achar nada.

5. "resumo_telegram" — string formatada pro Telegram com markdown (*negrito*). Estrutura:
   - Emoji + data
   - Linha com P/L + ROI
   - 1-2 destaques em bullets
   - Nada muito longo (máx 8 linhas)

Responda APENAS com o JSON, nada antes ou depois."""

    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )

    text = resp.content[0].text.strip()
    # Remove markdown code fences se vierem
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return json.loads(text)

# ============================================================
# GERAR CARD DO TWITTER (imagem 1200x675)
# ============================================================
def gerar_card_twitter(data_ref, dia, mes, acum, frase):
    W, H = 1200, 675
    img = Image.new('RGB', (W, H), color=(10, 10, 15))
    draw = ImageDraw.Draw(img)

    # Tenta carregar JetBrains Mono, fallback pra fonte padrão
    try:
        font_mono_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 16)
        font_mono_mid   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 20)
        font_mono_big   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 44)
        font_sans_big   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 48)
    except:
        font_mono_small = ImageFont.load_default()
        font_mono_mid = ImageFont.load_default()
        font_mono_big = ImageFont.load_default()
        font_sans_big = ImageFont.load_default()

    # Cores
    muted = (107, 107, 144)
    green = (0, 212, 170)
    red   = (255, 77, 106)
    white = (232, 232, 245)

    # Topo
    draw.text((64, 56), "MERCADO ESPORTIVO", font=font_mono_small, fill=muted)
    dt = datetime.strptime(data_ref, '%Y-%m-%d')
    draw.text((W - 200, 56), dt.strftime('%d · %m · %y'), font=font_mono_small, fill=muted)

    # Frase central (quebra em 2 linhas se preciso)
    y_frase = 210
    max_w = W - 128
    words = frase.split(' ')
    linha1, linha2 = [], []
    cur = []
    for w in words:
        test = ' '.join(cur + [w])
        bbox = draw.textbbox((0,0), test, font=font_sans_big)
        if bbox[2] - bbox[0] > max_w - 100:
            linha1 = cur
            cur = [w]
        else:
            cur.append(w)
    if linha1:
        linha2 = cur
    else:
        linha1 = cur

    draw.text((64, y_frase), ' '.join(linha1), font=font_sans_big, fill=white)
    if linha2:
        draw.text((64, y_frase + 62), ' '.join(linha2), font=font_sans_big, fill=muted)

    # Linha divisória
    draw.line([(64, H - 200), (W - 64, H - 200)], fill=(26, 26, 46), width=1)

    # 3 colunas de números
    cols = [
        ("HOJE", dia['plU'], dia['roiU']),
        ("MÊS", mes['plU'], mes['roiU']),
        ("ACUMULADO", acum['plU'], acum['roiU']),
    ]
    col_width = (W - 128) // 3
    for i, (label, plu, roi) in enumerate(cols):
        x = 64 + i * col_width
        draw.text((x, H - 170), label, font=font_mono_small, fill=muted)
        color = green if plu >= 0 else red
        draw.text((x, H - 140), fmtU(plu), font=font_mono_big, fill=color)
        draw.text((x, H - 82), f"ROI {pct(roi)}", font=font_mono_small, fill=muted)

    # Rodapé
    draw.text((W - 200, H - 40), "@evvol_bettor", font=font_mono_small, fill=muted)

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf

# ============================================================
# GERAR HTML DO BRIEFING COMPLETO
# ============================================================
def gerar_html_briefing(data_ref, dia, mes, acum, tops_dia, conteudo):
    dt = datetime.strptime(data_ref, '%Y-%m-%d')
    dia_ptbr = dt.strftime('%d de %B de %Y')

    def bloco(titulo_cor, titulo, texto, cor_texto):
        return f'''<div style="background:#141729;border:1px solid #2a2a45;border-radius:12px;padding:20px 24px;margin-bottom:16px;border-left:3px solid {cor_texto}">
  <div style="font-size:11px;color:{cor_texto};letter-spacing:0.08em;text-transform:uppercase;font-family:monospace;margin-bottom:8px">{titulo_cor}</div>
  <div style="font-size:16px;font-weight:500;margin-bottom:6px;color:#e8e8f5">{titulo}</div>
  <p style="font-size:14px;color:#b0b0c5;margin:0;line-height:1.6">{texto}</p>
</div>'''

    blocos = []
    dp = conteudo.get('destaque_positivo')
    if dp:
        blocos.append(bloco("DESTAQUE", dp.get('titulo',''), dp.get('texto',''), "#00d4aa"))
    da = conteudo.get('destaque_alerta')
    if da:
        blocos.append(bloco("ALERTA", da.get('titulo',''), da.get('texto',''), "#ffd84d"))
    cur = conteudo.get('curiosidade')
    if cur:
        blocos.append(bloco("VOCÊ SABIA", cur.get('titulo',''), cur.get('texto',''), "#6c63ff"))

    plcolor = '#00d4aa' if dia['plU'] >= 0 else '#ff4d6a'

    return f'''<div style="max-width:720px;margin:0 auto;padding:24px;font-family:system-ui,sans-serif;background:#0a0a0f;color:#e8e8f5">
  <div style="border-bottom:1px solid #2a2a45;padding-bottom:16px;margin-bottom:24px">
    <div style="font-size:11px;color:#6b6b90;letter-spacing:0.12em;text-transform:uppercase;font-family:monospace;margin-bottom:4px">Mercado Esportivo · Daily Briefing</div>
    <h1 style="font-size:24px;font-weight:500;margin:0">{dia_ptbr}</h1>
  </div>

  <h2 style="font-size:18px;font-weight:500;margin:0 0 12px">Fechamento do dia</h2>
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px">
    <div style="background:#16162a;border-radius:8px;padding:14px">
      <div style="font-size:11px;color:#6b6b90;text-transform:uppercase;font-family:monospace">Entradas</div>
      <div style="font-size:22px;font-weight:500;margin-top:4px">{dia['entradas']}</div>
    </div>
    <div style="background:#16162a;border-radius:8px;padding:14px">
      <div style="font-size:11px;color:#6b6b90;text-transform:uppercase;font-family:monospace">P/L</div>
      <div style="font-size:22px;font-weight:500;margin-top:4px;color:{plcolor}">{fmtU(dia['plU'])}</div>
      <div style="font-size:11px;color:#6b6b90;margin-top:2px">{fmtR(dia['plR'])}</div>
    </div>
    <div style="background:#16162a;border-radius:8px;padding:14px">
      <div style="font-size:11px;color:#6b6b90;text-transform:uppercase;font-family:monospace">ROI</div>
      <div style="font-size:22px;font-weight:500;margin-top:4px;color:{plcolor}">{pct(dia['roiU'])}</div>
    </div>
    <div style="background:#16162a;border-radius:8px;padding:14px">
      <div style="font-size:11px;color:#6b6b90;text-transform:uppercase;font-family:monospace">Acerto</div>
      <div style="font-size:22px;font-weight:500;margin-top:4px">{dia['acerto']:.1f}%</div>
    </div>
  </div>

  {''.join(blocos)}

  <h2 style="font-size:18px;font-weight:500;margin:24px 0 12px">Mês vs. acumulado</h2>
  <div style="background:#16162a;border-radius:8px;padding:16px 20px;margin-bottom:16px">
    <table style="width:100%;font-size:14px;color:#e8e8f5">
      <tr>
        <td style="padding:6px 0;color:#6b6b90">Mês</td>
        <td style="padding:6px 0;text-align:right;font-weight:500">{fmtU(mes['plU'])}</td>
        <td style="padding:6px 0;text-align:right;color:#6b6b90;width:120px">ROI {pct(mes['roiU'])}</td>
      </tr>
      <tr style="border-top:1px solid #2a2a45">
        <td style="padding:6px 0;color:#6b6b90">Acumulado (desde 01/06/2025)</td>
        <td style="padding:6px 0;text-align:right;font-weight:500">{fmtU(acum['plU'])}</td>
        <td style="padding:6px 0;text-align:right;color:#6b6b90">ROI {pct(acum['roiU'])}</td>
      </tr>
    </table>
  </div>

  <div style="border-top:1px solid #2a2a45;padding-top:16px;font-size:12px;color:#6b6b90;text-align:center;font-family:monospace;margin-top:24px">
    Gerado automaticamente · {acum['entradas']} registros desde 01/06/2025
  </div>
</div>'''

# ============================================================
# POSTAR NO TWITTER
# ============================================================
def postar_twitter(texto, imagem_bytes):
    from requests_oauthlib import OAuth1
    auth = OAuth1(TW_API_KEY, TW_API_SECRET, TW_ACCESS_TOKEN, TW_ACCESS_SECRET)
    imagem_bytes.seek(0)
    img_bytes = imagem_bytes.read()

    # Tenta vários endpoints de upload (X mudou recentemente)
    endpoints = [
        'https://upload.twitter.com/1.1/media/upload.json',
        'https://upload.x.com/1.1/media/upload.json',
        'https://api.x.com/2/media/upload',
        'https://upload.x.com/2/media/upload',
    ]

    media_id = None
    last_err = None
    for url in endpoints:
        try:
            print(f"  Tentando upload em: {url}")
            files = {'media': ('card.png', img_bytes, 'image/png')}
            r = requests.post(url, auth=auth, files=files, timeout=60)
            print(f"    Status: {r.status_code}")
            if r.ok:
                data = r.json()
                media_id = (data.get('data', {}).get('id') or
                            data.get('media_id_string') or
                            str(data.get('media_id')) if data.get('media_id') else None or
                            data.get('id'))
                if media_id:
                    print(f"    ✅ Media ID: {media_id}")
                    break
            else:
                last_err = f"{r.status_code}: {r.text[:200]}"
                print(f"    ❌ {last_err}")
        except Exception as e:
            last_err = str(e)
            print(f"    ❌ Exception: {e}")

    if not media_id:
        raise Exception(f"Upload falhou em todos endpoints. Último erro: {last_err}")

    # Posta o tweet via tweepy v2
    client = tweepy.Client(
        consumer_key=TW_API_KEY, consumer_secret=TW_API_SECRET,
        access_token=TW_ACCESS_TOKEN, access_token_secret=TW_ACCESS_SECRET
    )
    resp = client.create_tweet(text=texto, media_ids=[media_id])
    return resp.data.get('id') if resp.data else None

# ============================================================
# ENDPOINT PRINCIPAL
# ============================================================
@app.route('/', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'briefing-bot'})

@app.route('/test-twitter', methods=['GET'])
def test_twitter():
    """Testa as credenciais do Twitter postando um tweet de texto simples."""
    try:
        from requests_oauthlib import OAuth1
        client = tweepy.Client(
            consumer_key=TW_API_KEY, consumer_secret=TW_API_SECRET,
            access_token=TW_ACCESS_TOKEN, access_token_secret=TW_ACCESS_SECRET
        )
        # Primeiro tenta verificar identidade
        me_url = 'https://api.x.com/2/users/me'
        auth = OAuth1(TW_API_KEY, TW_API_SECRET, TW_ACCESS_TOKEN, TW_ACCESS_SECRET)
        r = requests.get(me_url, auth=auth, timeout=15)
        me_info = {'status': r.status_code, 'body': r.text[:500]}

        # Tenta postar um tweet de teste
        from datetime import datetime as dt2
        texto = f"Teste automatizado {dt2.now().strftime('%H:%M:%S')}"
        tweet_resp = None
        tweet_err = None
        try:
            resp = client.create_tweet(text=texto)
            tweet_resp = {'id': resp.data.get('id'), 'text': texto}
        except Exception as e:
            tweet_err = str(e)

        return jsonify({
            'keys_preview': {
                'api_key': TW_API_KEY[:6] + '...',
                'access_token': TW_ACCESS_TOKEN[:20] + '...',
            },
            'users_me': me_info,
            'tweet_test': tweet_resp,
            'tweet_error': tweet_err,
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500

@app.route('/fechar-dia', methods=['POST', 'OPTIONS'])
def fechar_dia():
    # CORS
    if request.method == 'OPTIONS':
        return ('', 204, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type'
        })

    headers_cors = {'Access-Control-Allow-Origin': '*'}

    try:
        body = request.get_json() or {}
        data_ref = body.get('data_ref') or datetime.now(BRT).strftime('%Y-%m-%d')
        usuario  = body.get('usuario', 'Sistema')
        postar_tw = body.get('postar_twitter', True)

        print(f"\n🔄 Fechando dia {data_ref} por {usuario}...")

        # 1. Buscar todos os dados
        print("  → Buscando apostas...")
        apostas = sb_get(f'apostas?select=data_evento,stake_unidades,lucro_unidades,status,tipster_id,bookie_id,operador_id,odd&limit=100000')
        stakes = sb_get('stakes_historico?select=tipster_id,valor_reais,vigente_a_partir')
        tipsters = sb_get('tipsters?select=id,nome')
        bookies = sb_get('bookies?select=id,nome')
        operadores = sb_get('operadores?select=id,nome')

        print(f"  → {len(apostas)} apostas, {len(stakes)} stakes, {len(tipsters)} tipsters")

        # 2. Agregar períodos
        dt_ref = datetime.strptime(data_ref, '%Y-%m-%d').date()
        mes_de  = dt_ref.replace(day=1).isoformat()
        mes_ate = data_ref

        dia  = agregar_periodo(apostas, stakes, data_ref, data_ref)
        mes  = agregar_periodo(apostas, stakes, mes_de, mes_ate)
        acum = agregar_periodo(apostas, stakes, INICIO_OPERACAO, data_ref)

        tops_dia = tops_periodo(apostas, stakes, data_ref, data_ref, tipsters, bookies, operadores)
        tops_mes = tops_periodo(apostas, stakes, mes_de, mes_ate, tipsters, bookies, operadores)

        print(f"  → Dia: {fmtU(dia['plU'])} | Mês: {fmtU(mes['plU'])} | Acum: {fmtU(acum['plU'])}")

        # 3. Gerar conteúdo via Claude
        print("  → Chamando Claude...")
        conteudo = gerar_conteudo_claude(data_ref, dia, mes, acum, tops_dia, tops_mes)
        print(f"  → Frase: {conteudo.get('frase_twitter','')[:60]}")

        # 4. Gerar HTML do briefing
        html = gerar_html_briefing(data_ref, dia, mes, acum, tops_dia, conteudo)

        # 5. Salvar no Supabase
        print("  → Salvando briefing no Supabase...")
        sb_upsert('briefings', {
            'data_ref': data_ref,
            'resumo_telegram': conteudo.get('resumo_telegram', ''),
            'html_completo': html,
            'frase_twitter': conteudo.get('frase_twitter', ''),
            'destaques_json': {
                'positivo': conteudo.get('destaque_positivo'),
                'alerta': conteudo.get('destaque_alerta'),
                'curiosidade': conteudo.get('curiosidade'),
            },
            'metricas_json': {'dia': dia, 'mes': mes, 'acum': acum},
            'criado_por': usuario,
        })

        # 6. Disparar Telegram
        print("  → Enviando Telegram...")
        msg_tg = conteudo.get('resumo_telegram', '') + f"\n\n📊 [Ver briefing completo]({DASH_URL}#briefing/{data_ref})"
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg_tg, 'parse_mode': 'Markdown', 'disable_web_page_preview': True},
            timeout=15
        ).raise_for_status()

        # 7. Postar Twitter
        tweet_id = None
        if postar_tw:
            try:
                print("  → Gerando card do Twitter...")
                card = gerar_card_twitter(data_ref, dia, mes, acum, conteudo.get('frase_twitter', ''))
                texto_tw = f"Fechamento de {dt_ref.strftime('%d/%m')}\n\n{conteudo.get('frase_twitter','')}"
                if len(texto_tw) > 270:
                    texto_tw = conteudo.get('frase_twitter','')[:270]
                print("  → Postando no Twitter...")
                tweet_id = postar_twitter(texto_tw, card)
                if tweet_id:
                    sb_upsert('briefings', {'data_ref': data_ref, 'twitter_post_id': str(tweet_id)})
                    print(f"  ✅ Tweet postado: {tweet_id}")
            except Exception as e:
                print(f"  ⚠️ Erro Twitter: {e}")
                traceback.print_exc()

        print("✅ Fechamento concluído!\n")

        return jsonify({
            'ok': True,
            'data_ref': data_ref,
            'dia': {'plU': dia['plU'], 'roiU': dia['roiU'], 'entradas': dia['entradas']},
            'twitter_id': str(tweet_id) if tweet_id else None,
        }), 200, headers_cors

    except Exception as e:
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500, headers_cors


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
