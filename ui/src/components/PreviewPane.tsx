import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { OverlayData, PreviewData } from "../types";

interface Props {
  sourcePath: string;
  preview: PreviewData | null;
  loading: boolean;
  overlay: OverlayData | null;
  showLines: boolean;
  showTexts: boolean;
  onToggleLines: () => void;
  onToggleTexts: () => void;
  onOpenImage: () => void;
}

const MIN_ZOOM = 0.05;
const MAX_ZOOM = 8;

export default function PreviewPane(props: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const imageRef = useRef<HTMLImageElement | null>(null);
  // Bumped when the preview PNG finishes decoding, so drawing always happens
  // through the effect below with CURRENT props — calling draw() directly from
  // onload would capture a stale overlay closure.
  const [imageVersion, setImageVersion] = useState(0);
  // null = fit-to-pane; a number = explicit scale of the preview bitmap.
  const [zoom, setZoom] = useState<number | null>(null);

  const fileName = useMemo(() => {
    const path = props.sourcePath.replace(/\\/g, "/");
    return path.split("/").pop() ?? "";
  }, [props.sourcePath]);

  // Decode the preview PNG once per preview payload; new image resets to fit.
  useEffect(() => {
    imageRef.current = null;
    setZoom(null);
    if (!props.preview) return;
    const image = new Image();
    image.onload = () => {
      imageRef.current = image;
      setImageVersion((version) => version + 1);
    };
    image.src = `data:image/png;base64,${props.preview.png_base64}`;
  }, [props.preview]);

  const draw = () => {
    const canvas = canvasRef.current;
    const preview = props.preview;
    const image = imageRef.current;
    if (!canvas || !preview || !image) return;
    canvas.width = preview.preview_width;
    canvas.height = preview.preview_height;
    const context = canvas.getContext("2d");
    if (!context) return;
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0);

    const overlay = props.overlay;
    if (!overlay) return;
    const scale = preview.scale;
    if (props.showLines) {
      context.strokeStyle = "#2563a8";
      context.lineWidth = 1.4;
      context.beginPath();
      overlay.lines.forEach((line) => {
        line.forEach(([x, y], index) => {
          const px = x * scale;
          const py = y * scale;
          if (index === 0) context.moveTo(px, py);
          else context.lineTo(px, py);
        });
      });
      context.stroke();
    }
    if (props.showTexts) {
      context.strokeStyle = "#b7791f";
      context.lineWidth = 1.2;
      context.font = "11px Consolas, monospace";
      context.fillStyle = "#7a5210";
      overlay.texts.forEach((box) => {
        const x = box.left * scale;
        const y = box.top * scale;
        const width = (box.right - box.left) * scale;
        const height = (box.bottom - box.top) * scale;
        context.strokeRect(x, y, width, height);
        if (box.text && width > 24) context.fillText(box.text, x + 2, Math.max(10, y - 3));
      });
    }
  };

  useEffect(draw, [imageVersion, props.overlay, props.showLines, props.showTexts, props.preview]);

  // Current on-screen scale of the preview bitmap (fit mode measures the DOM).
  const effectiveZoom = useCallback((): number => {
    if (zoom !== null) return zoom;
    const canvas = canvasRef.current;
    const preview = props.preview;
    if (!canvas || !preview || preview.preview_width === 0) return 1;
    const rect = canvas.getBoundingClientRect();
    return rect.width / preview.preview_width || 1;
  }, [zoom, props.preview]);

  const applyZoom = useCallback(
    (next: number, anchor?: { clientX: number; clientY: number }) => {
      const viewport = viewportRef.current;
      const clamped = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, next));
      const previous = effectiveZoom();
      let anchorState: { contentX: number; contentY: number; offsetX: number; offsetY: number } | null = null;
      if (viewport && anchor) {
        const rect = viewport.getBoundingClientRect();
        anchorState = {
          contentX: viewport.scrollLeft + (anchor.clientX - rect.left),
          contentY: viewport.scrollTop + (anchor.clientY - rect.top),
          offsetX: anchor.clientX - rect.left,
          offsetY: anchor.clientY - rect.top,
        };
      }
      setZoom(clamped);
      // Keep the point under the cursor stationary: adjust scroll after the
      // new size has been laid out.
      requestAnimationFrame(() => {
        if (!viewport || !anchorState || previous <= 0) return;
        const ratio = clamped / previous;
        viewport.scrollLeft = anchorState.contentX * ratio - anchorState.offsetX;
        viewport.scrollTop = anchorState.contentY * ratio - anchorState.offsetY;
      });
    },
    [effectiveZoom],
  );

  // Wheel zoom needs a non-passive listener to preventDefault the scroll.
  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const onWheel = (event: WheelEvent) => {
      if (!props.preview) return;
      event.preventDefault();
      const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15;
      applyZoom(effectiveZoom() * factor, { clientX: event.clientX, clientY: event.clientY });
    };
    viewport.addEventListener("wheel", onWheel, { passive: false });
    return () => viewport.removeEventListener("wheel", onWheel);
  }, [applyZoom, effectiveZoom, props.preview]);

  // Drag to pan.
  const dragState = useRef<{ x: number; y: number; left: number; top: number } | null>(null);
  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    const viewport = viewportRef.current;
    if (!viewport || event.button !== 0 || !props.preview) return;
    dragState.current = {
      x: event.clientX,
      y: event.clientY,
      left: viewport.scrollLeft,
      top: viewport.scrollTop,
    };
    viewport.setPointerCapture(event.pointerId);
  };
  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const viewport = viewportRef.current;
    const start = dragState.current;
    if (!viewport || !start) return;
    viewport.scrollLeft = start.left - (event.clientX - start.x);
    viewport.scrollTop = start.top - (event.clientY - start.y);
  };
  const onPointerUp = (event: React.PointerEvent<HTMLDivElement>) => {
    dragState.current = null;
    viewportRef.current?.releasePointerCapture(event.pointerId);
  };

  const overlayCount = props.overlay ? props.overlay.lines.length : 0;
  const textCount = props.overlay ? props.overlay.texts.length : 0;
  const zoomPercent = props.preview && props.preview.scale > 0
    ? Math.round(effectiveZoom() * props.preview.scale * 100)
    : 100;

  return (
    <div className="center-pane">
      <div className="preview-header">
        <span className="name">{fileName || "未选择影像"}</span>
        {props.preview && (
          <span className="dims">
            px=({props.preview.source_width}, {props.preview.source_height})
          </span>
        )}
        {props.preview && (
          <span className="zoom-group">
            <button className="zoom-btn" title="缩小" onClick={() => applyZoom(effectiveZoom() / 1.25)}>
              −
            </button>
            <span className="zoom-label" title="相对原图的显示比例">
              {zoom === null ? `适应 ${zoomPercent}%` : `${zoomPercent}%`}
            </span>
            <button className="zoom-btn" title="放大" onClick={() => applyZoom(effectiveZoom() * 1.25)}>
              ＋
            </button>
            <button
              className="zoom-btn text"
              title="按原图 1:1 像素显示"
              onClick={() => props.preview && applyZoom(1 / (props.preview.scale || 1))}
            >
              1:1
            </button>
            <button className="zoom-btn text" title="适应窗口" onClick={() => setZoom(null)}>
              适应
            </button>
          </span>
        )}
        <span style={{ flex: 1 }} />
        {props.overlay && (
          <>
            <button className={`chip ${props.showLines ? "on" : ""}`} onClick={props.onToggleLines}>
              线候选 {overlayCount}
              {props.overlay.dropped_lines > 0 ? `（另有 ${props.overlay.dropped_lines} 条未显示）` : ""}
            </button>
            <button className={`chip ${props.showTexts ? "on" : ""}`} onClick={props.onToggleTexts}>
              文字候选 {textCount}
            </button>
          </>
        )}
        <span className="chip static">review only · checked=no</span>
      </div>
      <div
        className="preview-viewport"
        ref={viewportRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        style={props.preview ? { cursor: dragState.current ? "grabbing" : "grab" } : undefined}
      >
        {props.preview ? (
          <div
            className="preview-canvas-wrap"
            style={
              zoom === null
                ? { maxWidth: "100%" }
                : { width: Math.round(props.preview.preview_width * zoom) }
            }
          >
            <canvas
              ref={canvasRef}
              style={{ display: "block", width: "100%", height: "auto" }}
            />
          </div>
        ) : props.loading ? (
          <div className="preview-empty">
            <h2>正在读取影像…</h2>
            <p>大图（几百 MB 的 TIFF）需要几秒钟生成预览。</p>
          </div>
        ) : (
          <div className="preview-empty">
            <h2>从一张扫描图开始</h2>
            <p>
              ① 打开影像（Map ID 自动识别）→ ② 在“运行参数”里选择 MapGIS / DXF / QGIS 输出 → ③
              点“开始”。完成后可在底部“输出文件”或输出目录查看成果；线/文字候选会叠加显示在这里，最终以人工复核为准。
            </p>
            <button className="btn primary" onClick={props.onOpenImage}>
              打开影像…
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
