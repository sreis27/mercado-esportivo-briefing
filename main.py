"""
Briefing Diário - Mercado Esportivo
Endpoint HTTP que gera o briefing, salva no Supabase, dispara no Telegram e posta no Twitter.
"""

import os, io, traceback
from datetime import datetime, timezone, timedelta, date
from flask import Flask, request, jsonify
import requests
from PIL import Image, ImageDraw, ImageFont
import tweepy

# ============================================================
# CREDENCIAIS
# ============================================================
SUPABASE_URL     = "https://yfdrifvhsiumdxgypkjm.supabase.co"
SUPABASE_KEY     = os.environ.get("SUPABASE_KEY", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-4659428992")
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

MESES_PT = ['', 'janeiro','fevereiro','março','abril','maio','junho','julho','agosto','setembro','outubro','novembro','dezembro']
def mes_pt(dt):
    return MESES_PT[dt.month]
def data_extenso_pt(dt):
    return f"{dt.day} de {MESES_PT[dt.month]} de {dt.year}"

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

def tops_grupos_periodo(apostas, stakes, de, ate, cache_tipsters):
    """Agrupa apostas settled do período por tipster (= grupo) com plU e plR."""
    filtradas = [a for a in apostas if de <= (a.get('data_evento') or '') <= ate and a.get('status') != 'PENDING']
    mp = {}
    for a in filtradas:
        tid = a.get('tipster_id')
        if not tid:
            continue
        if tid not in mp:
            item = next((c for c in cache_tipsters if c['id'] == tid), None)
            mp[tid] = {'nome': item['nome'] if item else 'Outros', 'plU': 0.0, 'plR': 0.0, 'n': 0}
        lu = float(a.get('lucro_unidades') or 0)
        sv = get_stake_valor(tid, a.get('data_evento'), stakes) or 1
        mp[tid]['plU'] += lu
        mp[tid]['plR'] += lu * sv
        mp[tid]['n']   += 1
    return list(mp.values())

# ============================================================
# GERAR RESUMO DO TELEGRAM (sem IA)
# ============================================================
def gerar_resumo_telegram(data_ref, dia, mes, acum, top_u, top_r):
    """Monta o texto do Telegram em markdown a partir das métricas — sem IA."""
    dt = datetime.strptime(data_ref, '%Y-%m-%d')
    data_fmt = dt.strftime('%d/%m/%Y')

    linhas = [
        f"📊 *Fechamento de {data_fmt}*",
        "",
        f"*P/L:* {fmtU(dia['plU'])} ({fmtR(dia['plR'])})",
        f"*ROI:* {pct(dia['roiU'])} u · {pct(dia['roiR'])} R$",
        f"*Entradas:* {dia['entradas']} · acerto {dia['acerto']:.1f}%",
    ]

    if top_u:
        linhas.append("")
        linhas.append("🏆 *Top 3 — Unidades*")
        medals = ['🥇', '🥈', '🥉']
        for i, g in enumerate(top_u[:3]):
            linhas.append(f"{medals[i]} {g['nome']}: {fmtU(g['plU'])}")

    linhas.append("")
    linhas.append(f"📅 *Mês:* {fmtU(mes['plU'])} (ROI {pct(mes['roiU'])})")
    linhas.append(f"📈 *Acumulado:* {fmtU(acum['plU'])} (ROI {pct(acum['roiU'])})")

    return '\n'.join(linhas)

# ============================================================
# GERAR CARD DO TWITTER (imagem 1200x675)
# ============================================================
def gerar_card_twitter(data_ref, dia, mes, acum, top_u, top_r):
    W, H = 1200, 675
    img = Image.new('RGB', (W, H), color=(10, 10, 15))
    draw = ImageDraw.Draw(img)

    try:
        f_xs   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 14)
        f_sm   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 16)
        f_md   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 20)
        f_lbl  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 12)
        f_big  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 36)
        f_xl   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 44)
        f_h1   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
    except:
        f_xs = f_sm = f_md = f_lbl = f_big = f_xl = f_h1 = ImageFont.load_default()

    muted = (107, 107, 144)
    green = (0, 212, 170)
    red   = (255, 77, 106)
    white = (232, 232, 245)
    accent = (108, 99, 255)

    # Topo
    draw.text((64, 40), "MERCADO ESPORTIVO", font=f_sm, fill=muted)
    dt = datetime.strptime(data_ref, '%Y-%m-%d')
    data_str = dt.strftime('%d · %m · %y')
    bbox = draw.textbbox((0,0), data_str, font=f_sm)
    draw.text((W - 64 - (bbox[2]-bbox[0]), 40), data_str, font=f_sm, fill=muted)

    draw.text((64, 75), f"Fechamento de {dt.day} de {mes_pt(dt)}", font=f_h1, fill=white)

    # Linha
    draw.line([(64, 135), (W - 64, 135)], fill=(42, 42, 69), width=1)

    # 4 cards principais (Entradas, P/L, ROI, Acerto)
    cards = [
        ('ENTRADAS', f"{dia['entradas']}",           None,                   white),
        ('P/L',      fmtU(dia['plU']),                fmtR(dia['plR']),       green if dia['plU'] >= 0 else red),
        ('ROI',      pct(dia['roiU']) + ' u',         pct(dia['roiR']) + ' R$', green if dia['roiU'] >= 0 else red),
        ('ACERTO',   f"{dia['acerto']:.1f}%",         f"{dia['won']}/{dia['settled']}", white),
    ]
    card_w = (W - 128 - 36) // 4  # 4 cards, 12px gap
    y_card = 160
    for i, (label, v1, v2, cor) in enumerate(cards):
        x = 64 + i * (card_w + 12)
        # bg
        draw.rounded_rectangle([(x, y_card), (x + card_w, y_card + 110)], radius=8, fill=(22, 22, 42))
        draw.text((x + 14, y_card + 14), label, font=f_lbl, fill=muted)
        draw.text((x + 14, y_card + 36), v1, font=f_big, fill=cor)
        if v2:
            draw.text((x + 14, y_card + 84), v2, font=f_xs, fill=muted)

    # Top 3 grupos — Unidades e Financeiro lado a lado
    y_top = 295
    col_w = (W - 128 - 16) // 2
    medals = ['1.', '2.', '3.']  # texto puro evita problema de fonte emoji
    medal_colors = [(255, 216, 77), (192, 192, 192), (205, 127, 50)]

    def desenhar_bloco_top(x0, titulo, lista, val_fn):
        draw.rounded_rectangle([(x0, y_top), (x0 + col_w, y_top + 220)], radius=10, fill=(20, 23, 41))
        draw.line([(x0, y_top), (x0, y_top + 220)], fill=accent, width=3)
        draw.text((x0 + 16, y_top + 14), titulo, font=f_lbl, fill=accent)
        if not lista:
            draw.text((x0 + 16, y_top + 50), 'Sem dados no dia.', font=f_sm, fill=muted)
            return
        for i, g in enumerate(lista[:3]):
            yr = y_top + 50 + i * 52
            draw.text((x0 + 18, yr), medals[i], font=f_md, fill=medal_colors[i])
            nome = g['nome']
            # trunca nome se muito longo
            max_nome_w = col_w - 200
            while True:
                bb = draw.textbbox((0,0), nome, font=f_sm)
                if bb[2] - bb[0] <= max_nome_w or len(nome) <= 6:
                    break
                nome = nome[:-2] + '…' if not nome.endswith('…') else nome[:-2]
            draw.text((x0 + 56, yr + 4), nome, font=f_sm, fill=white)
            valor, raw = val_fn(g)
            cor = green if raw >= 0 else red
            bb = draw.textbbox((0,0), valor, font=f_md)
            draw.text((x0 + col_w - 16 - (bb[2]-bb[0]), yr), valor, font=f_md, fill=cor)

    top_u_sorted = sorted(top_u, key=lambda g: g['plU'], reverse=True)
    top_r_sorted = sorted(top_r, key=lambda g: g['plR'], reverse=True)
    desenhar_bloco_top(64,                  'TOP 3 — UNIDADES',    top_u_sorted, lambda g: (fmtU(g['plU']), g['plU']))
    desenhar_bloco_top(64 + col_w + 16,     'TOP 3 — FINANCEIRO',  top_r_sorted, lambda g: (fmtR(g['plR']), g['plR']))

    # Footer: linha + 3 colunas Hoje / Mês / Acumulado
    draw.line([(64, H - 145), (W - 64, H - 145)], fill=(42, 42, 69), width=1)
    cols = [
        ("HOJE",      dia['plU'],  dia['roiU']),
        ("MÊS",       mes['plU'],  mes['roiU']),
        ("ACUMULADO", acum['plU'], acum['roiU']),
    ]
    col_w_f = (W - 128) // 3
    for i, (label, plu, roi) in enumerate(cols):
        x = 64 + i * col_w_f
        draw.text((x, H - 120), label, font=f_lbl, fill=muted)
        cor = green if plu >= 0 else red
        draw.text((x, H - 95), fmtU(plu), font=f_xl, fill=cor)
        draw.text((x, H - 40), f"ROI {pct(roi)}", font=f_sm, fill=muted)

    # @evvol_bettor
    handle = "@evvol_bettor"
    bb = draw.textbbox((0,0), handle, font=f_sm)
    draw.text((W - 64 - (bb[2]-bb[0]), H - 40), handle, font=f_sm, fill=muted)

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf

# ============================================================
# GERAR HTML DO BRIEFING COMPLETO
# ============================================================
def gerar_html_briefing(data_ref, dia, mes, acum, top_u, top_r):
    dt = datetime.strptime(data_ref, '%Y-%m-%d')
    dia_ptbr = data_extenso_pt(dt)

    plcolor = '#00d4aa' if dia['plU'] >= 0 else '#ff4d6a'
    roicolor = '#00d4aa' if dia['roiU'] >= 0 else '#ff4d6a'

    def card(label, valor_principal, valor_sub=None, cor=None):
        cor_v = cor or '#e8e8f5'
        sub_html = f'<div style="font-size:11px;color:#6b6b90;margin-top:2px">{valor_sub}</div>' if valor_sub else ''
        return f'''<div style="background:#16162a;border-radius:8px;padding:14px">
      <div style="font-size:11px;color:#6b6b90;text-transform:uppercase;font-family:monospace">{label}</div>
      <div style="font-size:20px;font-weight:500;margin-top:4px;color:{cor_v}">{valor_principal}</div>
      {sub_html}
    </div>'''

    cards_topo = ''.join([
        card('Entradas', f"{dia['entradas']}"),
        card('Investido', fmtU(dia['invU']), fmtR(dia['invR'])),
        card('P/L', fmtU(dia['plU']), fmtR(dia['plR']), plcolor),
        card('ROI', pct(dia['roiU']) + ' u', pct(dia['roiR']) + ' R$', roicolor),
        card('Acerto', f"{dia['acerto']:.1f}%", f"{dia['won']}/{dia['settled']}"),
        card('Mês', fmtU(mes['plU']), f"ROI {pct(mes['roiU'])}"),
    ])

    medals = ['🥇', '🥈', '🥉']

    def linha_top(g, i, valor_str, cor):
        return f'''<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #1e1e35">
      <span style="width:24px;text-align:center;font-size:14px">{medals[i] if i < 3 else (i+1)}</span>
      <span style="flex:1;color:#e8e8f5;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{g['nome']}</span>
      <span style="font-family:monospace;font-weight:500;color:{cor};font-size:14px">{valor_str}</span>
    </div>'''

    def bloco_top(titulo, lista, val_fn):
        if not lista:
            inner = '<div style="color:#6b6b90;font-size:13px;padding:8px 0">Sem dados no dia.</div>'
        else:
            inner = ''.join(linha_top(g, i, val_fn(g), '#00d4aa' if val_fn(g, raw=True) >= 0 else '#ff4d6a') for i, g in enumerate(lista[:3]))
        return f'''<div style="background:#141729;border:1px solid #2a2a45;border-radius:12px;padding:20px 24px;border-left:3px solid #6c63ff">
      <div style="font-size:11px;color:#6c63ff;letter-spacing:0.08em;text-transform:uppercase;font-family:monospace;margin-bottom:10px">{titulo}</div>
      {inner}
    </div>'''

    def val_u(g, raw=False): return g['plU'] if raw else fmtU(g['plU'])
    def val_r(g, raw=False): return g['plR'] if raw else fmtR(g['plR'])

    bloco_u = bloco_top('Top 3 — Unidades',   sorted(top_u, key=lambda g: g['plU'], reverse=True), val_u)
    bloco_r = bloco_top('Top 3 — Financeiro', sorted(top_r, key=lambda g: g['plR'], reverse=True), val_r)

    return f'''<div style="max-width:720px;margin:0 auto;padding:24px;font-family:system-ui,sans-serif;background:#0a0a0f;color:#e8e8f5">
  <div style="border-bottom:1px solid #2a2a45;padding-bottom:16px;margin-bottom:24px">
    <div style="font-size:11px;color:#6b6b90;letter-spacing:0.12em;text-transform:uppercase;font-family:monospace;margin-bottom:4px">Mercado Esportivo · Daily Briefing</div>
    <h1 style="font-size:24px;font-weight:500;margin:0">{dia_ptbr}</h1>
  </div>

  <h2 style="font-size:18px;font-weight:500;margin:0 0 12px">Fechamento do dia</h2>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:24px">
    {cards_topo}
  </div>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
    {bloco_u}
    {bloco_r}
  </div>

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
        # O briefing só agrega a partir de INICIO_OPERACAO — buscar além disso é desperdício
        # e estourou o teto de 100k linhas em 06/09/2026 (tabela cruzou 100.000 apostas),
        # truncando o payload sem aviso. order=desc garante que, se um dia a janela
        # exceder o limit, as linhas cortadas sejam as mais antigas, nunca as do dia.
        apostas = sb_get(f'apostas?select=data_evento,stake_unidades,lucro_unidades,status,tipster_id,bookie_id,operador_id,odd&data_evento=gte.{INICIO_OPERACAO}&order=data_evento.desc&limit=100000')
        stakes = sb_get('stakes_historico?select=tipster_id,valor_reais,vigente_a_partir')
        tipsters = sb_get('tipsters?select=id,nome')

        print(f"  → {len(apostas)} apostas, {len(stakes)} stakes, {len(tipsters)} tipsters")

        # 2. Agregar períodos
        dt_ref = datetime.strptime(data_ref, '%Y-%m-%d').date()
        mes_de  = dt_ref.replace(day=1).isoformat()
        mes_ate = data_ref

        dia  = agregar_periodo(apostas, stakes, data_ref, data_ref)
        mes  = agregar_periodo(apostas, stakes, mes_de, mes_ate)
        acum = agregar_periodo(apostas, stakes, INICIO_OPERACAO, data_ref)

        grupos_dia = tops_grupos_periodo(apostas, stakes, data_ref, data_ref, tipsters)
        top_u = sorted(grupos_dia, key=lambda g: g['plU'], reverse=True)
        top_r = sorted(grupos_dia, key=lambda g: g['plR'], reverse=True)

        print(f"  → Dia: {fmtU(dia['plU'])} | Mês: {fmtU(mes['plU'])} | Acum: {fmtU(acum['plU'])}")

        # 3. Montar resumo do Telegram (sem IA)
        resumo_tg = gerar_resumo_telegram(data_ref, dia, mes, acum, top_u, top_r)

        # 4. Gerar HTML do briefing
        html = gerar_html_briefing(data_ref, dia, mes, acum, top_u, top_r)

        # 5. Salvar no Supabase
        print("  → Salvando briefing no Supabase...")
        sb_upsert('briefings', {
            'data_ref': data_ref,
            'resumo_telegram': resumo_tg,
            'html_completo': html,
            'frase_twitter': '',
            'destaques_json': {},
            'metricas_json': {'dia': dia, 'mes': mes, 'acum': acum},
            'criado_por': usuario,
        })

        # 6. Disparar Telegram
        print("  → Enviando Telegram...")
        msg_tg = resumo_tg + f"\n\n📊 [Ver briefing completo]({DASH_URL}#briefing/{data_ref})"
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': msg_tg, 'parse_mode': 'Markdown', 'disable_web_page_preview': True},
            timeout=15
        ).raise_for_status()

        # 7. Gerar card do Twitter e salvar como base64 para download manual
        tweet_id = None
        texto_tw = f"Fechamento de {dt_ref.strftime('%d/%m')}\n\nP/L {fmtU(dia['plU'])} · ROI {pct(dia['roiU'])} · {dia['entradas']} entradas"
        try:
            print("  → Gerando card do Twitter...")
            card = gerar_card_twitter(data_ref, dia, mes, acum, top_u, top_r)
            import base64
            card.seek(0)
            card_b64 = base64.b64encode(card.read()).decode('ascii')
            sb_upsert('briefings', {
                'data_ref': data_ref,
                'twitter_post_id': f"MANUAL::{texto_tw}",
            })
            print(f"  ✅ Card gerado ({len(card_b64)} chars base64)")
        except Exception as e:
            print(f"  ⚠️ Erro gerando card: {e}")
            traceback.print_exc()
            card_b64 = None

        print("✅ Fechamento concluído!\n")

        return jsonify({
            'ok': True,
            'data_ref': data_ref,
            'dia': {'plU': dia['plU'], 'roiU': dia['roiU'], 'entradas': dia['entradas']},
            'twitter_card_base64': card_b64,
            'twitter_text': texto_tw,
        }), 200, headers_cors

    except Exception as e:
        traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500, headers_cors


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)