"""Renders a self-contained HTML dashboard.

2026-07-29, second pass. Restructured to match the Telegram output: one row per
EVENT instead of separate "spikes" / "sharp vs public" / "Asia vs Europe"
tables. Those three views were three different angles on the same market move,
which meant a single match appeared in all three and the page read as more
crowded than the market actually was. They are now one table: price range
across the market, where the line moved, the computed fair price, and the entry
price the analyst derives from it.

Removed at the user's request: the "where the data comes from" card and the raw
"last snapshot" table (200 rows of unaggregated prices nobody reads). Source
provenance moved to a single compact footer line so it isn't lost entirely.
"""
import html
import os
from datetime import datetime, timedelta, timezone

from config import (
    DASHBOARD_PATH,
    SPIKE_THRESHOLD_PCT,
    POLL_INTERVAL_MINUTES,

)
import storage

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KEWA / Vilka / Tracker — трекер движения коэффициентов</title>
<style>
  /* Design tokens from the validated reference palette (dark surface #1a1a19).
     Deliberately NO display typeface: the previous build used Orbitron and a
     monospace face for body copy, which is what made it read as a 2000s skin.
     Everything sits in the system UI sans, per the palette's typography rule,
     with tabular figures only where columns must align. Status colours are the
     reserved good/warning/critical steps and ALWAYS ship with an icon and a
     text label -- red vs green measure only ΔE 4.1 under deuteranopia, so
     colour alone would be unreadable for a colour-blind user. */
  :root {{
    color-scheme: dark;
    --plane: #0d0d0d;
    --surface: #1a1a19;
    --surface-2: #211f1e;
    --ink: #ffffff;
    --ink-2: #c3c2b7;
    --muted: #898781;
    --hairline: rgba(255,255,255,0.10);
    --hairline-strong: rgba(255,255,255,0.18);
    --good: #0ca30c;
    --warning: #fab219;
    --critical: #d03b3b;
    --accent: #3987e5;
    --radius: 12px;
    --radius-sm: 8px;
  }}
  * {{ box-sizing: border-box; }}
  html {{ -webkit-text-size-adjust: 100%; }}
  body {{
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    margin: 0; padding: 32px 20px 72px; background: var(--plane); color: var(--ink);
    font-size: 15px; line-height: 1.55; -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 1120px; margin: 0 auto; }}

  /* Hero. The old header left a wide empty band on desktop; this fills it with
     the mark at a size that can actually be read as a logo, plus the live
     numbers, so the space earns its keep instead of being padding. */
  header.top {{
    position: relative; overflow: hidden;
    border: 1px solid var(--hairline); border-radius: 18px;
    padding: 34px 36px; margin-bottom: 20px;
    background:
      radial-gradient(900px 320px at 12% -30%, rgba(57,135,229,0.20), transparent 70%),
      radial-gradient(700px 300px at 92% 0%, rgba(250,178,25,0.10), transparent 70%),
      var(--surface);
  }}
  header.top::after {{
    content: ''; position: absolute; inset: 0; pointer-events: none;
    background-image: radial-gradient(rgba(255,255,255,0.05) 1px, transparent 1px);
    background-size: 22px 22px; mask-image: linear-gradient(180deg, #000, transparent 75%);
    -webkit-mask-image: linear-gradient(180deg, #000, transparent 75%);
  }}
  .hero {{ position: relative; z-index: 1; display: flex; align-items: center; gap: 26px; flex-wrap: wrap; }}
  .mark {{ flex: none; line-height: 0; filter: drop-shadow(0 10px 26px rgba(57,135,229,0.45)); }}
  .brand-text {{ flex: 1 1 320px; }}
  .brand-text h1 {{
    font-size: clamp(30px, 5vw, 46px); font-weight: 800; letter-spacing: -0.035em;
    margin: 0; line-height: 1.02;
  }}
  .brand-text h1 .sep {{ color: var(--accent); margin: 0 6px; }}
  .brand-text p {{
    margin: 10px 0 0; font-size: clamp(15px, 1.6vw, 18px); color: var(--ink-2); max-width: 46ch;
  }}
  .hero-stats {{ display: flex; gap: 26px; flex-wrap: wrap; margin-top: 18px; }}
  .hs b {{ display: block; font-size: 26px; font-weight: 750; letter-spacing: -0.02em; line-height: 1.1; }}
  .hs span {{ font-size: 12.5px; color: var(--muted); }}
  .hs.gold b {{ color: var(--warning); }}
  .hs.green b {{ color: var(--good); }}

  .status {{ flex: 0 0 250px; }}
  .pill {{
    display: inline-flex; align-items: center; gap: 7px; font-size: 12px; font-weight: 650;
    padding: 5px 12px; border-radius: 999px; border: 1px solid var(--hairline);
    letter-spacing: 0.03em; text-transform: uppercase;
  }}
  .pill.live {{ color: var(--good); border-color: rgba(12,163,12,0.4); background: rgba(12,163,12,0.12); }}
  .pill.stale {{ color: var(--warning); border-color: rgba(250,178,25,0.4); background: rgba(250,178,25,0.12); }}
  .dot {{ width: 7px; height: 7px; border-radius: 50%; background: currentColor; flex: none; }}
  .pill.live .dot {{ animation: pulse 2s ease-in-out infinite; }}
  @keyframes pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: 0.35; }} }}

  .countdown {{
    margin-top: 10px; background: var(--surface); border: 1px solid var(--hairline);
    border-radius: var(--radius-sm); padding: 11px 14px;
  }}
  .cd-row {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }}
  .cd-label {{ font-size: 12px; color: var(--muted); }}
  .cd-time {{
    font-size: 21px; font-weight: 700; letter-spacing: -0.02em;
    font-variant-numeric: tabular-nums; color: var(--ink);
  }}
  .cd-bar {{ height: 4px; border-radius: 999px; background: rgba(255,255,255,0.09); margin-top: 8px; overflow: hidden; }}
  .cd-bar i {{
    display: block; height: 100%; border-radius: 999px;
    background: linear-gradient(90deg, var(--accent), #6da7ec); transition: width 0.9s linear;
  }}
  .countdown small {{ display: block; margin-top: 7px; font-size: 12px; color: var(--muted); }}

  section {{
    background: var(--surface); border: 1px solid var(--hairline);
    border-radius: var(--radius); padding: 22px 24px; margin-bottom: 18px;
  }}
  section > h2 {{
    font-size: 12px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase;
    color: var(--muted); margin: 0 0 14px;
  }}
  .lede p {{ margin: 0 0 12px; color: var(--ink-2); max-width: 78ch; }}
  .lede p:last-child {{ margin-bottom: 0; }}
  .lede b {{ color: var(--ink); font-weight: 600; }}
  details.how {{ margin-top: 4px; }}
  details.how > summary {{
    cursor: pointer; list-style: none; color: var(--accent); font-size: 14px; font-weight: 550;
    padding: 6px 0;
  }}
  details.how > summary::-webkit-details-marker {{ display: none; }}
  details.how > summary::before {{ content: '▸ '; }}
  details.how[open] > summary::before {{ content: '▾ '; }}
  details.how .body {{ padding-top: 6px; }}

  .toolbar {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }}
  .f {{
    font: inherit; font-size: 13.5px; font-weight: 550; cursor: pointer;
    padding: 7px 14px; border-radius: 999px; color: var(--ink-2);
    background: transparent; border: 1px solid var(--hairline);
    transition: background 0.12s ease, color 0.12s ease, border-color 0.12s ease;
  }}
  .f:hover {{ color: var(--ink); border-color: var(--hairline-strong); background: rgba(255,255,255,0.04); }}
  .f .n {{ color: var(--muted); margin-left: 5px; }}
  .f.active {{ background: var(--ink); color: #0d0d0d; border-color: var(--ink); }}
  .f.active .n {{ color: rgba(13,13,13,0.55); }}

  .feed-wrap {{ overflow-x: auto; margin: 0 -8px; padding: 0 8px; }}
  table.feed {{ width: 100%; border-collapse: collapse; }}
  table.feed th {{
    text-align: left; padding: 0 12px 10px; color: var(--muted);
    font-size: 11.5px; font-weight: 550; letter-spacing: 0.04em; text-transform: uppercase;
    white-space: nowrap; border-bottom: 1px solid var(--hairline);
  }}
  table.feed td {{ padding: 13px 12px; border-bottom: 1px solid var(--hairline); vertical-align: middle; }}
  table.feed tr.row:hover td {{ background: rgba(255,255,255,0.03); }}
  table.feed tr.row:last-child td {{ border-bottom: none; }}
  .c-stars {{ white-space: nowrap; font-size: 13px; letter-spacing: -1px; width: 1%; }}
  .c-ev {{ font-weight: 600; letter-spacing: -0.01em; line-height: 1.3; }}
  .c-ev small {{ display: block; font-size: 12.5px; color: var(--muted); font-weight: 400; margin-top: 2px; }}
  .c-out {{ color: var(--ink-2); font-weight: 550; white-space: nowrap; }}
  .c-move {{ white-space: nowrap; font-variant-numeric: tabular-nums; font-size: 14.5px; }}
  .c-move .old {{ color: var(--ink); font-weight: 600; }}
  .c-move .arr {{ color: var(--muted); margin: 0 5px; }}
  .c-move .new {{ color: var(--muted); }}
  .c-move .pct {{
    display: inline-block; margin-left: 7px; font-size: 12px; font-weight: 600;
    color: var(--critical); background: rgba(208,59,59,0.13); padding: 2px 7px; border-radius: 6px;
  }}
  .c-books {{ color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; font-size: 14px; }}
  .c-bet {{ white-space: nowrap; }}
  .c-bet .price {{ color: var(--good); font-weight: 700; font-size: 17px; font-variant-numeric: tabular-nums; }}
  .c-bet small {{ display: block; font-size: 12.5px; color: var(--muted); margin-top: 1px; }}
  .c-bet.shut {{ color: var(--muted); font-size: 13px; }}

  .stat-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 16px; }}
  .stat {{ background: var(--surface-2); border: 1px solid var(--hairline); border-radius: var(--radius-sm); padding: 14px 16px; }}
  .stat b {{ display: block; font-size: 28px; font-weight: 650; letter-spacing: -0.02em; margin-bottom: 2px; }}
  .stat span {{ font-size: 13px; color: var(--muted); line-height: 1.35; display: block; }}

  .bank {{ border-radius: var(--radius-sm); padding: 18px 20px; margin-bottom: 18px; border: 1px solid var(--hairline); background: var(--surface-2); }}
  .bank.good {{ border-color: rgba(12,163,12,0.4); background: rgba(12,163,12,0.08); }}
  .bank.bad {{ border-color: rgba(208,59,59,0.4); background: rgba(208,59,59,0.08); }}
  .bank-head {{ font-size: 14px; color: var(--ink-2); }}
  .bank-head b {{ color: var(--ink); }}
  .bank-num {{ font-size: 40px; font-weight: 700; letter-spacing: -0.03em; margin: 6px 0 4px; }}
  .bank.good .bank-num {{ color: var(--good); }}
  .bank.bad .bank-num {{ color: var(--critical); }}
  .bank-sub {{ font-size: 14px; color: var(--ink-2); }}
  .bank-note {{ font-size: 12.5px; color: var(--muted); margin-top: 8px; }}

  .last5 {{ margin-top: 20px; }}
  .last5 > h3 {{ font-size: 12px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); margin: 0 0 10px; }}
  details.bet {{ border: 1px solid var(--hairline); border-radius: var(--radius-sm); margin-bottom: 8px; background: var(--surface-2); }}
  details.bet[open] {{ border-color: var(--hairline-strong); }}
  details.bet > summary {{
    cursor: pointer; padding: 13px 16px; list-style: none; display: flex;
    justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap;
  }}
  details.bet > summary::-webkit-details-marker {{ display: none; }}
  details.bet > summary:hover {{ background: rgba(255,255,255,0.03); border-radius: var(--radius-sm); }}
  .b-left {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .b-name {{ font-weight: 600; }}
  .b-pick {{ color: var(--muted); font-size: 13.5px; font-variant-numeric: tabular-nums; }}
  .b-body {{ padding: 4px 16px 16px; }}
  .b-body table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  .b-body td {{ padding: 8px 0; border-bottom: 1px solid var(--hairline); }}
  .b-body tr:last-child td {{ border-bottom: none; }}
  .b-body td:first-child {{ color: var(--muted); width: 210px; }}
  .num {{ font-variant-numeric: tabular-nums; }}

  /* Status chips: icon + word + colour, never colour alone. */
  .chip {{ display: inline-flex; align-items: center; gap: 5px; font-size: 12.5px; font-weight: 600; padding: 3px 9px; border-radius: 999px; white-space: nowrap; }}
  .chip.win {{ color: var(--good); background: rgba(12,163,12,0.13); }}
  .chip.lose {{ color: var(--critical); background: rgba(208,59,59,0.13); }}
  .chip.wait {{ color: var(--warning); background: rgba(250,178,25,0.13); }}
  .chip.na {{ color: var(--muted); background: rgba(255,255,255,0.06); }}
  .chip.open {{ color: var(--good); background: rgba(12,163,12,0.13); }}
  .chip.shut {{ color: var(--muted); background: rgba(255,255,255,0.06); }}

  table.plain {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  table.plain th {{ text-align: left; padding: 0 10px 9px; color: var(--muted); font-size: 11.5px; font-weight: 550; text-transform: uppercase; letter-spacing: 0.04em; border-bottom: 1px solid var(--hairline); }}
  table.plain td {{ padding: 11px 10px; border-bottom: 1px solid var(--hairline); }}
  table.plain tr:last-child td {{ border-bottom: none; }}

  .empty, .norows {{ color: var(--muted); font-size: 14px; padding: 18px 2px; }}
  .note {{ color: var(--ink-2); font-size: 14px; margin: 0 0 16px; max-width: 82ch; }}
  .note b {{ color: var(--ink); font-weight: 600; }}
  footer {{ text-align: center; color: var(--muted); font-size: 12.5px; margin-top: 34px; line-height: 1.8; }}
  /* Side rails. Purely decorative, so they are pointer-events:none, sit behind
     everything, and only appear when there is genuinely dead space either side
     of the 1120px column -- never on laptops or phones where they'd crowd the
     content. Wording is deliberately about patience, price and bankroll rather
     than "bet more": the entire point of the tool is to act on a rule instead
     of on impulse, and hype text on the wall would work against its owner. */
  .rail {{
    position: fixed; top: 0; bottom: 0; width: 250px; pointer-events: none; z-index: 0;
    display: none; flex-direction: column; justify-content: center; gap: 26px; padding: 24px;
  }}
  .rail.l {{ left: 0; align-items: flex-end; }}
  .rail.r {{ right: 0; align-items: flex-start; }}
  .sticker {{
    background: var(--surface); border: 1px solid var(--hairline-strong);
    border-radius: 14px; padding: 13px 16px; max-width: 205px;
    box-shadow: 0 10px 30px rgba(0,0,0,0.45); opacity: 0.62;
  }}
  .sticker .em {{ font-size: 20px; display: block; margin-bottom: 5px; }}
  .sticker .tx {{ font-size: 13.5px; font-weight: 650; line-height: 1.35; color: var(--ink); }}
  .sticker .sub {{ font-size: 11.5px; color: var(--muted); margin-top: 4px; }}
  .sticker.a {{ transform: rotate(-3.5deg); border-color: rgba(57,135,229,0.4); }}
  .sticker.b {{ transform: rotate(2.5deg); border-color: rgba(12,163,12,0.4); }}
  .sticker.c {{ transform: rotate(-1.5deg); border-color: rgba(250,178,25,0.4); }}
  .sticker.d {{ transform: rotate(3deg); border-color: rgba(208,59,59,0.35); }}
  @media (min-width: 1560px) {{ .rail {{ display: flex; }} }}

  @media (max-width: 640px) {{
    body {{ padding: 20px 14px 56px; }}
    section {{ padding: 18px 16px; }}
    header.top {{ padding: 24px 20px; border-radius: 14px; }}
    .hero {{ gap: 18px; }}
    .bank-num {{ font-size: 32px; }}
    .hero-stats {{ gap: 18px; }}
  }}
</style>
</head>
<body data-updated="{updated_iso}" data-interval="{poll_interval}">

<aside class="rail l" aria-hidden="true">
  <div class="sticker a"><span class="em">🎯</span><span class="tx">Мы не угадываем. Мы считаем.</span>
    <div class="sub">сигнал или ничего</div></div>
  <div class="sticker b"><span class="em">⏳</span><span class="tx">Пропустить — тоже решение</span>
    <div class="sub">нет входа — нет ставки</div></div>
  <div class="sticker c"><span class="em">📐</span><span class="tx">Плоская ставка. Всегда.</span>
    <div class="sub">банкролл важнее прогноза</div></div>
</aside>

<aside class="rail r" aria-hidden="true">
  <div class="sticker c"><span class="em">💸</span><span class="tx">Цену делает не матч, а деньги</span>
    <div class="sub">смотри, куда они пошли</div></div>
  <div class="sticker a"><span class="em">⭐</span><span class="tx">Три звезды бьют интуицию</span>
    <div class="sub">чем больше контор — тем вернее</div></div>
  <div class="sticker d"><span class="em">🚫</span><span class="tx">Не отыгрывайся</span>
    <div class="sub">рынок не должен тебе ничего</div></div>
</aside>

<div class="wrap">
  <header class="top">
    <div class="hero">
      <div class="mark" aria-hidden="true">
        <svg viewBox="0 0 64 64" width="104" height="104" role="img">
          <defs>
            <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stop-color="#3987e5"/><stop offset="1" stop-color="#16478a"/>
            </linearGradient>
          </defs>
          <rect x="0" y="0" width="64" height="64" rx="17" fill="url(#bg)"/>
          <!-- The fork is the joke and the thesis: "вилка" is both cutlery and
               the betting term for an arb, and its tines double as a rising line. -->
          <g stroke="#0d0d0d" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M20 15v11" fill="none" stroke="#fdfdfb"/>
            <path d="M27 13v13" fill="none" stroke="#fdfdfb"/>
            <path d="M34 15v11" fill="none" stroke="#fdfdfb"/>
            <path d="M18 26h18a2 2 0 0 1 2 2v2a11 11 0 0 1-8 10v10a3 3 0 0 1-6 0V40a11 11 0 0 1-8-10v-2a2 2 0 0 1 2-2z"
                  fill="#fdfdfb"/>
          </g>
          <circle cx="23.5" cy="32" r="2.5" fill="#0d0d0d"/>
          <circle cx="31.5" cy="32" r="2.5" fill="#0d0d0d"/>
          <circle cx="24.4" cy="31.2" r="0.9" fill="#fff"/>
          <circle cx="32.4" cy="31.2" r="0.9" fill="#fff"/>
          <path d="M23 37.5c1.7 2 6.3 2 8 0" fill="none" stroke="#0d0d0d"
                stroke-width="2.4" stroke-linecap="round"/>
          <path d="M49 16l-9 13h6l-3 12 10-14h-6z" fill="#fab219" stroke="#0d0d0d" stroke-width="2.6"
                stroke-linejoin="round"/>
        </svg>
      </div>

      <div class="brand-text">
        <h1>KEWA<span class="sep">·</span>VILKA<span class="sep">·</span>TRACKER</h1>
        <p>Ловим деньги раньше, чем их увидит рынок.</p>
        <div class="hero-stats">
          <div class="hs"><b>{hero_events}</b><span>событий в работе</span></div>
          <div class="hs green"><b>{hero_open}</b><span>с открытым входом</span></div>
          <div class="hs gold"><b>{hero_stars}</b><span>на три звезды</span></div>
          <div class="hs"><b>{hero_books}</b><span>контор в опросе</span></div>
        </div>
      </div>

      <div class="status">
        <span class="pill {freshness_class}"><span class="dot"></span>{freshness_label}</span>
        <div class="countdown">
          <div class="cd-row">
            <span class="cd-label">Обновление через</span>
            <span class="cd-time" id="cd">--:--</span>
          </div>
          <div class="cd-bar"><i id="cdbar" style="width:0%"></i></div>
          <small id="cdago">обновлено {updated_ago}</small>
        </div>
      </div>
    </div>
  </header>

  <section class="lede">
    <h2>Что это за продукт</h2>
    <p>Трекер каждые {poll_interval} минут снимает коэффициенты по всему рынку и ловит
    момент, когда на какой-то исход <b>занесли деньги</b>. Всё важное дублируется
    в Telegram.</p>
    <p>Если коэффициент был <b>3.00</b> и у нескольких контор просел до <b>2.10</b> —
    в этот исход зашли деньги. Ставим мы <b>на тот же исход</b>, но там, где цена ещё
    не упала: забираем старые 3.00, пока их дают.</p>
    <details class="how">
      <summary>Подробнее о логике и звёздах</summary>
      <div class="body">
        <p>Обратную сторону не рассматриваем никогда — она подорожала механически,
        просто потому что деньги пошли против неё.</p>
        <p>Главный фильтр — <b>сколько контор подвинулось</b>, а не насколько сильно.
        Одна контора может дёрнуть цену из-за чьей-то одиночной ставки или ошибки
        трейдера. Когда один и тот же исход просел сразу у многих независимых контор
        за полчаса — это уже информированные деньги. Отсюда звёзды: ⭐ одна контора,
        ⭐⭐ две-три, ⭐⭐⭐ четыре и больше либо с участием шарп-конторы.</p>
        <p>Работаем только по матчам <b>до старта</b>: в лайве цена скачет от голов,
        а не от денег. Ничья в футболе не рассматривается. Биржи не берём — там цену
        двигает один случайный человек. В бота уходят только падения
        от <b>{threshold_pct}%</b>, где ещё есть где поставить.</p>
      </div>
    </details>
  </section>

  <section>
    <h2>Сводка по рынку</h2>
    {summaries_html}
  </section>

  <section>
    <h2>🔴 Сводка по рынку LIVE</h2>
    <p class="note">Матчи, которые <b>уже идут</b>. Ставок отсюда мы не делаем и в бота
    это не уходит: в лайве цена двигается от голов, а не от денег, так что наша логика
    там не работает. Но одна вещь в лайве говорящая — когда конторы <b>сильно разошлись
    в цене</b> на один и тот же исход. Обычно это значит, что кто-то не успел
    переставить линию после гола. Ниже такие расхождения от 25%.</p>
    {live_table}
  </section>

  <section>
    <h2>Проверка сигналов</h2>
    <p class="note">Считается по ставкам, чьи матчи уже закончились, и всегда по
    <b>той цене, которую мы называли</b>. <b>CLV</b> — успели ли мы взять цену до того,
    как её срезал весь рынок. Статистика начата с чистого листа 29.07.2026.</p>
    {stats_card}
    {last_bets}
  </section>

  <footer>
    страница обновляется автоматически · время везде UTC<br>
    это расчёт по движению рынка, а не рекомендация · ставки — риск потерять деньги
  </footer>
</div>
{countdown_js}
</body>
</html>
"""

COUNTDOWN_JS = """<script>
/* Live countdown to the next poll. The page is a static file regenerated every
   cycle, so without this the "обновлено только что" line silently goes stale
   while looking fresh. Everything is derived from the timestamp stamped into
   <body>, so the clock stays honest even if the tab is left open for hours. */
(function () {
  var el = document.getElementById('cd'),
      bar = document.getElementById('cdbar'),
      ago = document.getElementById('cdago'),
      last = Date.parse(document.body.dataset.updated || ''),
      span = (parseInt(document.body.dataset.interval, 10) || 30) * 60000;
  if (!el || isNaN(last)) return;

  function words(min) {
    if (min < 1) return 'обновлено только что';
    if (min < 60) return 'обновлено ' + min + ' мин назад';
    var h = Math.floor(min / 60);
    return 'обновлено ' + h + ' ч ' + (min % 60) + ' мин назад';
  }

  function tick() {
    var now = Date.now(), left = last + span - now, elapsed = now - last;
    if (left <= 0) {
      el.textContent = 'вот-вот';
      bar.style.width = '100%';
    } else {
      var s = Math.floor(left / 1000);
      el.textContent = Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
      bar.style.width = Math.min(100, (elapsed / span) * 100).toFixed(1) + '%';
    }
    ago.textContent = words(Math.floor(elapsed / 60000));
  }
  tick();
  setInterval(tick, 1000);
})();
</script>
"""


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Russian noun agreement: 1 событие / 2-4 события / 5+ событий."""
    n = abs(int(n or 0))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fmt_dt(value) -> str:
    dt = _parse_iso(value)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "—"


def _fmt_start(value) -> str:
    dt = _parse_iso(value)
    return dt.strftime("%d.%m %H:%M UTC") if dt else "—"


def _ago(value, now=None) -> str:
    dt = _parse_iso(value)
    if not dt:
        return "—"
    now = now or datetime.now(timezone.utc)
    mins = int((now - dt).total_seconds() // 60)
    if mins < 0:
        return f"через {abs(mins)} мин"
    if mins < 1:
        return "только что"
    if mins < 60:
        return f"{mins} мин назад"
    hours = mins // 60
    if hours < 24:
        return f"{hours} ч {mins % 60} мин назад"
    return f"{hours // 24} дн назад"


def _event_row(s: dict) -> str:
    """One compact table row per event. Everything needed to act on it -- which
    side money went into, what the price was and is, how broad the move was, and
    where to still take it -- has to fit on a single line, so the whole feed is
    scannable without scrolling."""
    bet = s.get("bet") or {}
    stars = s.get("stars", 0)
    has_entry = s.get("has_entry")
    big = s.get("big_move")

    name = f"{html.escape(s.get('home_team') or '?')} — {html.escape(s.get('away_team') or '?')}"
    outcome = html.escape(bet.get("name") or "—")

    if has_entry:
        bet_cell = (f"<td class='c-bet'><span class='price'>{bet['entry_price']:.2f}</span>"
                    f"<small>{html.escape(bet['entry_book'])}</small></td>")
    else:
        # Icon + word, never colour alone: red and green measure only ΔE 4.1
        # apart under deuteranopia, so every state is also spelled out.
        bet_cell = "<td class='c-bet shut'><span class='chip shut'>⛔ закрыт</span></td>"

    return (
        f"<tr class='row' data-stars='{stars}' data-open='{1 if has_entry else 0}' "
        f"data-big='{1 if big else 0}'>"
        f"<td class='c-stars'>{'⭐' * stars}</td>"
        f"<td class='c-ev'>{name}<small>{_fmt_start(s.get('start_time'))}</small></td>"
        f"<td class='c-out'>{outcome}</td>"
        f"<td class='c-move'><span class='old'>{bet['old_price']:.2f}</span>"
        f"<span class='arr'>→</span><span class='new'>{bet['new_price']:.2f}</span>"
        f"<span class='pct'>−{abs(bet['drop_pct']):.0f}%</span></td>"
        f"<td class='c-books'>{bet['down_count']}/{bet['books_count']}</td>"
        f"{bet_cell}</tr>"
    )


FILTER_JS = """
<script>
(function () {
  var buttons = document.querySelectorAll('.f');
  var rows = document.querySelectorAll('tr.row');
  var empty = document.getElementById('norows');
  function apply(mode) {
    var shown = 0;
    rows.forEach(function (r) {
      var ok;
      if (mode === 'all') ok = true;
      else if (mode === 'open') ok = r.dataset.open === '1';
      else if (mode === 'big') ok = r.dataset.big === '1';
      else ok = r.dataset.stars === mode;
      r.style.display = ok ? '' : 'none';
      if (ok) shown++;
    });
    empty.style.display = shown ? 'none' : '';
  }
  buttons.forEach(function (b) {
    b.addEventListener('click', function () {
      buttons.forEach(function (x) { x.classList.remove('active'); });
      b.classList.add('active');
      apply(b.dataset.f);
    });
  });
})();
</script>
"""


def _summaries_html(summaries: list, limit: int = 120) -> str:
    shown = [s for s in summaries if s.get("bet")][:limit]
    if not shown:
        return ('<p class="empty">Сейчас движений нет — линии стоят на месте. '
                'Строки появятся, как только рынок начнёт двигаться.</p>')

    n3 = sum(1 for s in shown if s["stars"] >= 3)
    n2 = sum(1 for s in shown if s["stars"] == 2)
    n1 = sum(1 for s in shown if s["stars"] == 1)
    nopen = sum(1 for s in shown if s.get("has_entry"))
    nbig = sum(1 for s in shown if s.get("big_move"))

    filters = (
        "<div class='toolbar'>"
        f"<button class='f active' data-f='all'>Все<span class='n'>{len(shown)}</span></button>"
        f"<button class='f' data-f='3'>⭐⭐⭐<span class='n'>{n3}</span></button>"
        f"<button class='f' data-f='2'>⭐⭐<span class='n'>{n2}</span></button>"
        f"<button class='f' data-f='1'>⭐<span class='n'>{n1}</span></button>"
        f"<button class='f' data-f='open'>Есть вход<span class='n'>{nopen}</span></button>"
        "</div>"
    )

    head = ("<tr><th></th><th>Событие</th><th>Деньги на</th>"
            "<th>Был → стал</th><th>Контор</th><th>Ставим</th></tr>")
    body = "".join(_event_row(s) for s in shown)
    table = f"<div class='feed-wrap'><table class='feed'>{head}{body}</table></div>"
    empty = "<p class='norows' id='norows' style='display:none'>Под этот фильтр ничего не подошло.</p>"
    return filters + table + empty + FILTER_JS


def _bankroll_block(stats: dict) -> str:
    """Plain-language flat-stake result. Percentages are easy to misread; a
    balance in dollars is not."""
    stake = int(stats.get("stake") or 0)
    n = stats.get("graded_n") or 0
    if not n:
        return (f"<div class='bank neutral'><div class='bank-head'>💵 Если ставить "
                f"по ${stake} на каждый сигнал</div>"
                f"<div class='bank-sub'>Считать пока нечего — ни один матч с сигналом "
                f"ещё не сыгран. Как только сыграет, здесь появится баланс.</div></div>")

    profit = stats["profit"]
    staked = stats["staked"]
    roi = stats["roi_pct"]
    sign = "+" if profit >= 0 else "−"
    cls = "good" if profit > 0 else ("bad" if profit < 0 else "neutral")
    word = "заработали" if profit > 0 else ("потеряли" if profit < 0 else "вышли в ноль")
    return (
        f"<div class='bank {cls}'>"
        f"<div class='bank-head'>💵 Если бы вы ставили по <b>${stake}</b> на каждый сигнал</div>"
        f"<div class='bank-num'>{sign}${abs(profit):,.0f}</div>"
        f"<div class='bank-sub'>За {n} {_plural(n, 'сыгравшую ставку', 'сыгравшие ставки', 'сыгравших ставок')} "
        f"вы бы {word} <b>{sign}${abs(profit):,.0f}</b>. "
        f"Оборот ${staked:,.0f}, доходность {roi:+.1f}%.</div>"
        f"<div class='bank-note'>Это подсчёт по уже сыгравшим сигналам и по той цене, "
        f"которую мы называли. Не обещание будущего результата.</div>"
        f"</div>"
    ).replace(",", " ")


def _live_table(rows) -> str:
    if not rows:
        return ('<p class="empty">Сейчас в идущих матчах конторы не расходятся сильнее '
                'чем на 25% — всё стоит ровно.</p>')
    out = []
    for r in rows:
        event = f"{r['home_team']} — {r['away_team']}"
        out.append(
            f"<tr><td class='c-ev'>{html.escape(event)}"
            f"<small>{html.escape(str(r.get('sport_key') or ''))}</small></td>"
            f"<td class='c-out'>{html.escape(r['name'])}</td>"
            f"<td class='c-move'><span class='new'>{r['low']:.2f}</span>"
            f"<span class='arr'>…</span><span class='old'>{r['high']:.2f}</span></td>"
            f"<td class='c-books'>{r['median']:.2f}</td>"
            f"<td><span class='chip wait'>⚠️ {r['spread_pct']:.0f}%</span></td>"
            f"<td class='c-bet'><span class='price'>{r['high']:.2f}</span>"
            f"<small>{html.escape(r['outlier_book'])}</small></td></tr>"
        )
    head = ("<tr><th>Матч идёт</th><th>Исход</th><th>Разброс цен</th>"
            "<th>Медиана</th><th>Расхождение</th><th>Выбивается</th></tr>")
    return f"<div class='feed-wrap'><table class='feed'>{head}{''.join(out)}</table></div>"


def _stats_card(stats: dict):
    win_rate = stats["win_rate"]
    win_rate_html = f"{win_rate:.0f}%" if win_rate is not None else "—"
    avg_clv = stats.get("avg_clv_pct")
    avg_clv_html = f"{avg_clv * 100:+.1f}%" if avg_clv is not None else "—"
    clv_rate = stats.get("clv_continued_rate")
    clv_rate_html = f"{clv_rate:.0f}%" if clv_rate is not None else "—"
    total_word = _plural(stats['total'], "сигнал отправлен", "сигнала отправлено", "сигналов отправлено")
    resolved_word = _plural(stats['resolved'], "матч проверен", "матча проверено", "матчей проверено")
    pending_word = _plural(stats['pending'], "ждёт", "ждут", "ждут") + " результата"
    summary = f"""
    <div class="stat-row">
      <div class="stat"><b>{stats['total']}</b>{total_word}</div>
      <div class="stat"><b>{stats['resolved']}</b>{resolved_word}</div>
      <div class="stat"><b>{stats['pending']}</b>{pending_word}</div>
      <div class="stat"><b>{win_rate_html}</b>win rate</div>
      <div class="stat"><b>{avg_clv_html}</b>средний CLV</div>
    </div>
    """
    summary += _bankroll_block(stats)

    if not stats["recent"]:
        return summary + ('<p class="empty">Проверенных сигналов пока нет — появятся, '
                          'как только закончится первый матч с алертом.</p>')
    rows = []
    for r in stats["recent"]:
        result = r["result"]
        cls = "hit" if result == "hit" else ("miss" if result == "miss" else "")
        result_label = {"hit": "✅ зашла", "miss": "❌ не зашла",
                        "n/a": "н/д"}.get(result, result)
        clv_pct = r["clv_pct"]
        clv_html = f"{clv_pct * 100:+.1f}%" if clv_pct is not None else "—"
        clv_cls = "hit" if r["clv_continued"] == 1 else ("miss" if r["clv_continued"] == 0 else "")
        home, away = r["home_team"], r["away_team"]
        event = f"{home} — {away}" if home and away else str(r["fixture_id"])
        old_p = f"{r['old_price']:.2f}" if r["old_price"] else "—"
        new_p = f"{r['new_price']:.2f}" if r["new_price"] else "—"
        entry = f"{r['entry_price']:.2f}" if r["entry_price"] else "—"
        rows.append(
            f"<tr><td class='c-stars'>{'⭐' * (r['stars'] or 0)}</td>"
            f"<td><b>{html.escape(event)}</b></td>"
            f"<td>{html.escape(r['outcome_name'] or '')}</td>"
            f"<td class='mono'>{old_p} → {new_p}</td>"
            f"<td class='mono'><b>{entry}</b> <small>{html.escape(r['entry_book'] or '')}</small></td>"
            f"<td class='{cls}'>{result_label}</td><td class='{clv_cls}'>{clv_html}</td></tr>"
        )
    table = ("<table class='plain'><tr><th></th><th>Событие</th><th>Ставили на</th>"
             "<th>Был → стал</th><th>Поставили по</th>"
             "<th>Результат</th><th>CLV</th></tr>"
             + "".join(rows) + "</table>")
    return summary + table


def _last_bets(bets, limit: int = 5) -> str:
    """Clickable list of the most recent bets -- click one to unfold what
    happened to it."""
    if not bets:
        return ("<div class='last5'><h3>Последние ставки</h3>"
                "<p class='empty'>Ставок пока нет — появятся с первым сигналом.</p></div>")

    items = []
    for b in bets[:limit]:
        home, away = b["home_team"], b["away_team"]
        event = f"{home} — {away}" if home and away else str(b["fixture_id"])
        stars = "⭐" * (b["stars"] or 0)
        entry = f"{b['entry_price']:.2f}" if b["entry_price"] else "—"

        if b["resolved"]:
            status = {"hit": "<span class='hit'>✅ зашла</span>",
                      "miss": "<span class='miss'>❌ не зашла</span>",
                      "n/a": "<span class='b-status'>н/д</span>"}.get(
                          b["result"], f"<span class='b-status'>{html.escape(str(b['result']))}</span>")
        else:
            status = "<span class='pending'>⏳ ждём матч</span>"

        clv = f"{b['clv_pct'] * 100:+.1f}%" if b["clv_pct"] is not None else "—"
        old_p = f"{b['old_price']:.2f}" if b["old_price"] else "—"
        new_p = f"{b['new_price']:.2f}" if b["new_price"] else "—"

        items.append(
            "<details class='bet'><summary>"
            f"<span class='b-left'><span class='c-stars'>{stars}</span>"
            f"<span class='b-name'>{html.escape(event)}</span>"
            f"<span class='b-pick'>{html.escape(b['outcome_name'] or '')} @ {entry}</span></span>"
            f"{status}</summary>"
            "<div class='b-body'><table>"
            f"<tr><td>Ставили на</td><td><b>{html.escape(b['outcome_name'] or '')}</b></td></tr>"
            f"<tr><td>Коэффициент был</td><td class='mono'>{old_p}</td></tr>"
            f"<tr><td>Просел до</td><td class='mono'>{new_p}</td></tr>"
            f"<tr><td>Поставили по</td><td class='mono'><b>{entry}</b> — "
            f"{html.escape(b['entry_book'] or '')}</td></tr>"
            f"<tr><td>Просело у контор</td><td class='mono'>{b['down_count'] or 0} "
            f"из {b['books_count'] or 0}</td></tr>"
            f"<tr><td>Старт матча</td><td class='mono'>{_fmt_dt(b['start_time'])}</td></tr>"
            f"<tr><td>Сигнал зафиксирован</td><td class='mono'>{_fmt_dt(b['detected_at'])}</td></tr>"
            f"<tr><td>Результат</td><td>{status}</td></tr>"
            f"<tr><td>CLV</td><td class='mono'>{clv}</td></tr>"
            "</table></div></details>"
        )
    return ("<div class='last5'><h3>Последние ставки — нажми, чтобы увидеть результат</h3>"
            + "".join(items) + "</div>")


def render_dashboard(summaries: list, quota: dict = None, live_rows: list = None):
    meta = storage.snapshot_meta()
    if quota:
        meta["quota_used"] = quota.get("used")
        meta["quota_remaining"] = quota.get("remaining")

    now = datetime.now(timezone.utc)
    fetched = _parse_iso(meta.get("fetched_at"))
    # More than two poll intervals without a refresh means the scheduler is
    # stuck -- say so instead of showing a green "LIVE" badge over stale data.
    fresh = bool(fetched and (now - fetched) < timedelta(minutes=POLL_INTERVAL_MINUTES * 2 + 5))

    html_out = PAGE_TEMPLATE.format(
        updated_iso=(_parse_iso(meta.get("fetched_at")) or now).isoformat(),
        updated_ago=_ago(meta.get("fetched_at"), now),
        freshness_class="live" if fresh else "stale",
        freshness_label="в эфире" if fresh else "данные устарели",
        poll_interval=POLL_INTERVAL_MINUTES,
        threshold_pct=f"{SPIKE_THRESHOLD_PCT * 100:.0f}",
        hero_events=len(summaries or []),
        hero_open=sum(1 for s in (summaries or []) if s.get("has_entry")),
        hero_stars=sum(1 for s in (summaries or []) if s.get("stars", 0) >= 3),
        hero_books=len(meta.get("bookmakers") or []),
        summaries_html=_summaries_html(summaries or []),
        live_table=_live_table(live_rows or []),
        stats_card=_stats_card(storage.alert_stats()),
        last_bets=_last_bets(storage.recent_bets(5)),
        countdown_js=COUNTDOWN_JS,
    )
    # git does not track empty directories, so a fresh CI checkout has no
    # dashboard/ folder yet -- make sure it exists before writing.
    os.makedirs(os.path.dirname(DASHBOARD_PATH), exist_ok=True)
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html_out)
    return DASHBOARD_PATH
