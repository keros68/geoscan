PIXEL_TO_ORIGINAL_TIF_MM = 25.4 / 300.0


def _escape_ogr_label_text(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def mapgis_dxf_label_style(
    text: str,
    font: str,
    font_mm: float,
    *,
    coordinate_scale: float,
    final_mm_per_pixel: float = PIXEL_TO_ORIGINAL_TIF_MM,
    color: str = "#000000",
) -> str:
    ground_size = float(font_mm) * float(coordinate_scale) / float(final_mm_per_pixel)
    escaped_text = _escape_ogr_label_text(text)
    escaped_font = _escape_ogr_label_text(font or "SimSun")
    return f'LABEL(t:"{escaped_text}",f:"{escaped_font}",s:{ground_size:.6f}g,c:{color})'
