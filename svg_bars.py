"""
Módulo de barras SVG con anti-colisión de etiquetas y overflow controlado.
Todas las barras usan overflow:hidden en el SVG y lógica de posicionamiento
defensivo para que las etiquetas nunca se superpongan ni salgan del contenedor.
"""


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _pos(value, scale_min, scale_max, W=400):
    """Posición x en píxeles dentro del SVG."""
    if scale_max == scale_min:
        return 0
    pct = (value - scale_min) / (scale_max - scale_min)
    return round(_clamp(pct, 0, 1) * W, 1)


def _safe_labels(positions_and_texts, W=400, min_gap=22):
    """
    Recibe lista de (x, text, anchor, color, bold).
    Ajusta posiciones para evitar solapamiento, respetando bordes.
    Retorna lista de (x_final, text, anchor, color, bold).
    """
    if not positions_and_texts:
        return []

    items = sorted(positions_and_texts, key=lambda t: t[0])
    result = list(items)

    # Empuje simple hacia la derecha si se solapan
    for i in range(1, len(result)):
        prev_x = result[i-1][0]
        curr_x = result[i][0]
        if curr_x - prev_x < min_gap:
            result[i] = (prev_x + min_gap, *result[i][1:])

    # Clamp al borde derecho
    for i in range(len(result)-1, -1, -1):
        x = result[i][0]
        if x > W - 4:
            result[i] = (W - 4, *result[i][1:])

    return result


def svg_bar(value, range_low, range_high, scale_min, scale_max,
            fill_color, norm_color="rgba(41,128,185,0.22)",
            label_unit="", height=40, W=400,
            low_label=None, high_label=None):
    """
    Barra horizontal SVG con:
    - Zona normal sombreada
    - Línea vertical en el valor actual
    - Etiquetas anti-colisión
    - Overflow hidden
    """
    x_val  = _pos(value, scale_min, scale_max, W)
    x_low  = _pos(range_low, scale_min, scale_max, W)
    x_high = _pos(range_high, scale_min, scale_max, W)
    norm_w = max(0, x_high - x_low)

    lbl_left  = f"{scale_min}{label_unit}"
    lbl_right = f"{scale_max}{label_unit}"
    lbl_val   = f"{value}{label_unit}"
    lbl_low   = low_label or f"{range_low}"
    lbl_high  = high_label or f"{range_high}"

    # Anti-colisión: value, low, high
    raw = [
        (x_val,  lbl_val,  "middle", "#c0392b", True),
        (x_low,  lbl_low,  "middle", "#2980b9", False),
        (x_high, lbl_high, "middle", "#2980b9", False),
    ]
    placed = _safe_labels(raw, W, min_gap=24)

    tick_svgs = ""
    for (px, txt, anchor, color, bold) in placed:
        weight = "bold" if bold else "normal"
        tick_svgs += (
            f'<text x="{px}" y="{height-1}" font-size="8" fill="{color}" '
            f'font-family="sans-serif" text-anchor="{anchor}" '
            f'font-weight="{weight}">{txt}</text>\n'
        )

    BAR_TOP = 10; BAR_H = 16
    FILL_TOP = BAR_TOP + 2; FILL_H = BAR_H - 4
    LINE_TOP = BAR_TOP - 3; LINE_BOT = BAR_TOP + BAR_H + 3

    return f"""<svg width="100%" height="{height}" viewBox="0 0 {W} {height}" style="overflow:visible;display:block">
  <rect x="0" y="{BAR_TOP}" width="{W}" height="{BAR_H}" rx="3" fill="rgba(0,0,0,0.08)"/>
  <rect x="{x_low}" y="{BAR_TOP}" width="{norm_w}" height="{BAR_H}" fill="{norm_color}"/>
  <rect x="0" y="{FILL_TOP}" width="{x_val}" height="{FILL_H}" rx="2" fill="{fill_color}"/>
  <line x1="{x_low}" y1="{LINE_TOP}" x2="{x_low}" y2="{LINE_BOT}" stroke="#2980b9" stroke-width="1" stroke-dasharray="2,2" opacity="0.7"/>
  <line x1="{x_high}" y1="{LINE_TOP}" x2="{x_high}" y2="{LINE_BOT}" stroke="#2980b9" stroke-width="1" stroke-dasharray="2,2" opacity="0.7"/>
  <line x1="{x_val}" y1="{LINE_TOP}" x2="{x_val}" y2="{LINE_BOT}" stroke="#c0392b" stroke-width="2"/>
  <text x="2" y="{height-1}" font-size="8" fill="#9c9a92" font-family="sans-serif">{lbl_left}</text>
  <text x="{W-2}" y="{height-1}" font-size="8" fill="#9c9a92" font-family="sans-serif" text-anchor="end">{lbl_right}</text>
  {tick_svgs}
</svg>"""


def svg_ratio_bar(ratio, ratio_low=0.55, ratio_high=0.65,
                  scale_min=0.40, scale_max=0.80, W=400):
    """Barra del ratio AEC/AIC."""
    x_val  = _pos(ratio, scale_min, scale_max, W)
    x_low  = _pos(ratio_low, scale_min, scale_max, W)
    x_high = _pos(ratio_high, scale_min, scale_max, W)
    norm_w = max(0, x_high - x_low)

    BAR_TOP = 14; BAR_H = 12

    ticks_data = [
        (0.40, "0.40"), (0.50, "0.50"), (0.55, "0.55"),
        (0.65, "0.65"), (0.70, "0.70"), (0.80, "0.80"),
    ]
    tick_svgs = ""
    for v, lbl in ticks_data:
        px = _pos(v, scale_min, scale_max, W)
        tick_svgs += f'<text x="{px}" y="35" font-size="7.5" fill="#9c9a92" font-family="sans-serif" text-anchor="middle">{lbl}</text>\n'

    # Etiqueta del valor
    raw_val = [(x_val, f"{ratio}", "middle", "#c0392b", True)]
    placed = _safe_labels(raw_val, W, min_gap=0)
    val_x = placed[0][0] if placed else x_val

    return f"""<svg width="100%" height="40" viewBox="0 0 {W} 40" style="overflow:visible;display:block">
  <rect x="0" y="{BAR_TOP}" width="{W}" height="{BAR_H}" rx="2" fill="rgba(0,0,0,0.10)"/>
  <rect x="{x_low}" y="{BAR_TOP}" width="{norm_w}" height="{BAR_H}" fill="rgba(41,128,185,0.25)"/>
  <line x1="{x_low}" y1="{BAR_TOP-2}" x2="{x_low}" y2="{BAR_TOP+BAR_H+2}" stroke="#2980b9" stroke-width="1" stroke-dasharray="2,2" opacity="0.7"/>
  <line x1="{x_high}" y1="{BAR_TOP-2}" x2="{x_high}" y2="{BAR_TOP+BAR_H+2}" stroke="#2980b9" stroke-width="1" stroke-dasharray="2,2" opacity="0.7"/>
  <line x1="{x_val}" y1="{BAR_TOP-3}" x2="{x_val}" y2="{BAR_TOP+BAR_H+3}" stroke="#c0392b" stroke-width="2"/>
  <text x="{val_x}" y="{BAR_TOP-2}" font-size="8" fill="#c0392b" font-family="sans-serif" text-anchor="middle" font-weight="bold">{ratio}</text>
  {tick_svgs}
  <text x="2" y="35" font-size="7.5" fill="#e74c3c" font-family="sans-serif">↓ deshidratación</text>
  <text x="{W-2}" y="35" font-size="7.5" fill="#e67e22" font-family="sans-serif" text-anchor="end">↑ retención</text>
</svg>"""


def svg_imme_bar(imme, sarco_thresh, normal_thresh, patient_name="", scale_max=20, W=400):
    """Barra IMME con zonas rojo/naranja/verde."""
    sarco_pct  = sarco_thresh / scale_max
    normal_pct = normal_thresh / scale_max
    val_pct    = _clamp(imme / scale_max, 0, 1)

    x_sarco  = round(sarco_pct  * W, 1)
    x_normal = round(normal_pct * W, 1)
    x_val    = round(val_pct    * W, 1)

    ticks = "".join([
        f'<text x="{round(i/scale_max*W,1)}" y="35" font-size="7.5" fill="#9c9a92" '
        f'font-family="sans-serif" text-anchor="middle">{i}</text>'
        for i in range(0, scale_max+1, 2)
    ])

    lbl_name = patient_name or f"IMME {imme}"

    return f"""<div style="position:relative;padding-top:20px;margin-bottom:3px">
  <div style="position:absolute;top:2px;left:{val_pct*100:.1f}%;transform:translateX(-50%);
              font-size:9px;font-weight:500;white-space:nowrap;color:#1a1a18">{lbl_name} {imme}</div>
  <div style="position:absolute;top:0;bottom:0;left:calc({val_pct*100:.1f}% - 1px);
              width:2px;background:#1a1a18;border-radius:1px;top:16px;bottom:0"></div>
  <svg width="100%" height="38" viewBox="0 0 {W} 38" style="display:block">
    <rect x="0" y="0" width="{x_sarco}" height="14" fill="#e74c3c" opacity="0.6"/>
    <rect x="{x_sarco}" y="0" width="{x_normal-x_sarco}" height="14" fill="#e67e22" opacity="0.7"/>
    <rect x="{x_normal}" y="0" width="{W-x_normal}" height="14" fill="#27ae60" opacity="0.2"/>
    <line x1="{x_sarco}" y1="0" x2="{x_sarco}" y2="14" stroke="#e74c3c" stroke-width="1.5" opacity="0.8"/>
    <line x1="{x_normal}" y1="0" x2="{x_normal}" y2="14" stroke="#e67e22" stroke-width="1" opacity="0.8"/>
    {ticks}
  </svg>
  <div style="display:flex;font-size:8px;margin-top:2px">
    <div style="width:{sarco_pct*100:.1f}%;color:#e74c3c;text-align:center">Sarcopenia &lt;{sarco_thresh}</div>
    <div style="width:{(normal_pct-sarco_pct)*100:.1f}%;color:#e67e22;text-align:center">↑</div>
    <div style="width:{(1-normal_pct)*100:.1f}%;color:#27ae60;text-align:center">Normal ≥{normal_thresh} kg/m²</div>
  </div>
</div>"""


def seg_muscle_row(name, kg, ideal_kg, quality, q_range, show_quality=True):
    """Fila segmental de músculo. show_quality=False para tronco (Tanita no mide MQ)."""
    ref_low  = round(ideal_kg * 0.8, 1) if ideal_kg > 0 else 0
    ref_high = round(ideal_kg * 1.2, 1) if ideal_kg > 0 else 0

    if show_quality:
        q_lo, q_hi = q_range
        if quality > q_hi:
            q_cat, q_color = "↑ Norm", "#8e44ad"
        elif quality < q_lo:
            q_cat, q_color = "Bajo", "#c0392b"
        else:
            q_cat, q_color = "Normal", "#2980b9"
        score_cell = f'<div style="font-size:10px;font-weight:500;text-align:right">{int(quality)}</div>'
        cat_cell   = f'<div style="font-size:9px;text-align:center;color:{q_color}">{q_cat}</div>'
        ref_cell   = f'<div class="seg-det">score ({q_lo}–{q_hi})</div>'
    else:
        score_cell = '<div style="font-size:9px;text-align:right;color:#9c9a92">—</div>'
        cat_cell   = '<div style="font-size:9px;text-align:center;color:#9c9a92">—</div>'
        ref_cell   = '<div class="seg-det" style="color:#9c9a92">sin dato</div>'

    range_text = f'{ref_low}–{ref_high} kg' if ideal_kg > 0 else '—'

    return f"""<div style="display:grid;grid-template-columns:75px 55px 70px 42px 55px 1fr;gap:3px;align-items:center;margin-bottom:4px">
  <div class="seg-name">{name}</div>
  <div style="font-size:10px;font-weight:500">{kg} kg</div>
  <div style="font-size:9px;color:#5c5b55">{range_text}</div>
  {score_cell}
  {cat_cell}
  {ref_cell}
</div>"""


def physique_svg(n: int, svg_w=52, svg_h=90) -> str:
    """
    SVG de perfil lateral para physique type 1-9.

    Grasa (silueta exterior azul claro): varía por FILA — (n-1)//3
      Misma forma de perfil que el músculo, escalada hacia afuera.
    Músculo (silueta interior azul oscuro): varía por COLUMNA — (n-1)%3
    """
    row = (n - 1) // 3   # 0=obeso, 1=normal, 2=delgado
    col = (n - 1) % 3    # 0=angosto, 1=medio, 2=ancho

    # Músculo — solo por columna: (fd=profundidad frente, bd=dorsal)
    muscle_by_col = [(3, 2), (6, 4), (9, 5)]
    fd, bd = muscle_by_col[col]

    # Grasa — padding adicional sobre músculo, solo por fila
    # (fp_f=extra frente, fp_b=extra espalda, fp_a=extra abdomen, hr_fat=radio cabeza grasa)
    fat_pad_by_row = [(7, 5, 5, 7), (5, 3, 3, 6), (3, 2, 1, 5)]
    fp_f, fp_b, fp_a, hr_fat = fat_pad_by_row[row]
    fd_fat = fd + fp_f
    bd_fat = bd + fp_b

    abd     = [4, 2, 0][row]          # abdomen muscular (sigue fila)
    abd_fat = abd + fp_a               # abdomen grasa (siempre mayor)

    cx = 26
    fx     = cx - fd;      bx     = cx + bd;      ax     = fx - abd
    fx_fat = cx - fd_fat;  bx_fat = cx + bd_fat;  ax_fat = max(0, fx_fat - abd_fat)

    yNk, ySh, yCh = 17, 21, 28
    yWa, yBe, yGr = 36, 44, 52
    yTh, yKn, yAn, yFt = 62, 71, 81, 87

    def body_path(fxp, bxp, axp, abdp):
        return " ".join([
            f"M {cx-1},{yNk}",
            f"L {bxp-1},{yNk}",
            f"Q {bxp},{ySh} {bxp},{yCh}",
            f"Q {bxp+1},{yWa} {bxp},{yBe}",
            f"Q {bxp},{yGr} {bxp-2},{yGr+4}",
            f"L {cx+1},{yFt}",
            f"L {fxp},{yFt}",
            f"L {fxp},{yAn}",
            f"L {fxp},{yKn}",
            f"L {fxp+1},{yTh}",
            f"L {fxp+1},{yGr}",
            f"Q {axp},{yBe} {axp},{yWa+4}",
            f"Q {fxp-1 if abdp else fxp},{yCh} {fxp},{ySh}",
            f"L {cx-1},{yNk}", "Z",
        ])

    d_fat    = body_path(fx_fat, bx_fat, ax_fat, abd_fat)
    d_muscle = body_path(fx,     bx,     ax,     abd)

    parts = [
        f'<path d="{d_fat}" fill="#BFDBFE" stroke="#93C5FD" stroke-width="0.8"/>',
        f'<circle cx="{cx}" cy="10" r="{hr_fat}" fill="#BFDBFE" stroke="#93C5FD" stroke-width="0.8"/>',
        f'<path d="{d_muscle}" fill="#1E40AF"/>',
        f'<circle cx="{cx}" cy="10" r="5" fill="#1E40AF"/>',
    ]

    return (
        f'<svg viewBox="0 0 52 90" width="{svg_w}" height="{svg_h}" '
        f'xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>'
    )


def seg_fat_row(name, fat_pct, ref_low, ref_high, ideal):
    """Fila segmental de grasa."""
    if fat_pct < ref_low:
        cat, cat_c = "↓ Atlética", "#27ae60"
    elif fat_pct > ref_high:
        cat, cat_c = "Alto", "#c0392b"
    else:
        cat, cat_c = "Normal", "#2980b9"

    return f"""<div style="display:grid;grid-template-columns:75px 48px 70px 1fr;gap:3px;align-items:center;margin-bottom:4px">
  <div class="seg-name">{name}</div>
  <div class="seg-pct">{fat_pct}%</div>
  <div class="seg-cat" style="color:{cat_c}">{cat}</div>
  <div class="seg-det">ref {ref_low}–{ref_high}%<br>ideal {ideal}%</div>
</div>"""
