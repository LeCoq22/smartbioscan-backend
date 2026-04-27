"""
Genera el gráfico de evolución histórica como imagen PNG embebida en base64.
Se usa en lugar de Chart.js para que aparezca correctamente en el PDF.
"""

import base64
import io
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from datetime import datetime


def build_evolution_chart_b64(evolution: dict) -> str:
    """
    Retorna una imagen PNG del gráfico de evolución en base64.
    Lista para embeber en HTML como <img src="data:image/png;base64,...">
    """
    series = evolution.get('series', [])
    if len(series) < 2:
        return ""

    recent = series[-10:]  # hasta 10 puntos

    def fmt_date(d):
        dt = datetime.strptime(d, "%Y-%m-%d")
        meses = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic']
        return f"{meses[dt.month-1]} {str(dt.year)[2:]}"

    labels  = [fmt_date(s['date']) for s in recent]
    weights = [s['weight_kg'] for s in recent]
    muscles = [s['muscle_kg'] for s in recent]
    fats    = [s['fat_pct'] for s in recent]
    xs      = list(range(len(labels)))

    # Colores del reporte
    c_weight = '#2c3e50'
    c_muscle = '#2980b9'
    c_fat    = '#e67e22'
    c_bg     = '#f4f2eb'
    c_text   = '#5c5b55'

    fig, ax1 = plt.subplots(figsize=(6.5, 2.4))
    fig.patch.set_facecolor(c_bg)
    ax1.set_facecolor(c_bg)

    # Eje izquierdo: Peso
    l1, = ax1.plot(xs, weights, color=c_weight, marker='o', markersize=4,
                   linewidth=1.5, label='Peso kg')
    ax1.set_ylabel('Peso kg', fontsize=8, color=c_text)
    ax1.tick_params(axis='y', labelcolor=c_text, labelsize=7)
    w_min = min(weights); w_max = max(weights)
    ax1.set_ylim(w_min - 1.5, w_max + 1.5)
    ax1.spines['top'].set_visible(False)
    ax1.spines['right'].set_visible(False)
    ax1.spines['left'].set_color(c_text)
    ax1.spines['bottom'].set_color(c_text)
    ax1.tick_params(axis='x', labelsize=7, colors=c_text)
    ax1.set_xticks(xs)
    ax1.set_xticklabels(labels, rotation=30, ha='right')
    ax1.grid(axis='y', color=(0,0,0,0.08), linewidth=0.5, linestyle='--')

    # Eje derecho: Músculo
    ax2 = ax1.twinx()
    l2, = ax2.plot(xs, muscles, color=c_muscle, marker='o', markersize=4,
                   linewidth=1.5, label='Músculo kg')
    ax2.set_ylabel('Musc. kg', fontsize=8, color=c_text)
    ax2.tick_params(axis='y', labelcolor=c_text, labelsize=7)
    m_min = min(muscles); m_max = max(muscles)
    ax2.set_ylim(m_min - 1.5, m_max + 1.5)
    ax2.spines['top'].set_visible(False)
    ax2.spines['right'].set_color(c_text)

    # Grasa % — eje derecho secundario (invisible, solo la línea)
    ax3 = ax1.twinx()
    ax3.spines['right'].set_position(('axes', 1.12))
    l3, = ax3.plot(xs, fats, color=c_fat, marker='o', markersize=4,
                   linewidth=1.2, linestyle='--', label='Grasa %')
    ax3.set_visible(False)  # ocultar eje, solo mostrar línea

    # Leyenda
    lines = [l1, l2, l3]
    labels_leg = ['Peso kg', 'Músculo kg', 'Grasa %']
    ax1.legend(lines, labels_leg, fontsize=7, loc='upper left',
               framealpha=0.7, facecolor='white', edgecolor='none',
               ncol=3, handlelength=1.5, columnspacing=1)

    plt.tight_layout(pad=0.5)

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight',
                facecolor=c_bg)
    plt.close(fig)
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode('utf-8')
    return f"data:image/png;base64,{b64}"
