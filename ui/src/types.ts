export type StageKey =
  | "00_INPUT_FREEZE"
  | "04_LINE_WORKFLOW"
  | "05_TEXT_WORKFLOW"
  | "DXF_EXPORT"
  | "08_SECTION_W60"
  | "MAPGIS_LOAD_READY";

export type StageState =
  | "pending"
  | "running"
  | "completed"
  | "warning"
  | "failed"
  | "skipped"
  | "blocked"
  | "cancelled";

export const STAGE_ORDER: StageKey[] = [
  "00_INPUT_FREEZE",
  "04_LINE_WORKFLOW",
  "05_TEXT_WORKFLOW",
  "DXF_EXPORT",
  "08_SECTION_W60",
  "MAPGIS_LOAD_READY",
];

export const STAGE_LABELS: Record<StageKey, string> = {
  "00_INPUT_FREEZE": "输入冻结",
  "04_LINE_WORKFLOW": "线候选",
  "05_TEXT_WORKFLOW": "文字候选",
  DXF_EXPORT: "交换文件",
  "08_SECTION_W60": "MapGIS 转换",
  MAPGIS_LOAD_READY: "交付包",
};

export interface RunForm {
  source_raster: string;
  map_id: string;
  project_root: string;
  output_parent: string;
  level_input: "off" | "auto" | "force";
  enhanced_preview: "none" | "light" | "standard" | "strong";
  conversion_mode: "none" | "prepare" | "cli";
  include_areas: boolean;
  reset_output: boolean;
  line_engine: "hough" | "trace";
  line_connect: "conservative" | "standard" | "aggressive";
  line_repair: "off" | "conservative";
  line_export_source: "raw" | "repaired" | "ai_enhanced";
  line_bridge_gap_px: string;
  line_close_gap_px: string;
  export_dxf: boolean;
  wait_timeout_seconds: string;
  target_line_file: string;
  target_text_file: string;
  target_area_file: string;
  text_candidates: string;
  // AI（可选，review-only）。api_key 仅会话内存在：留空时引擎自动用本机加密保存的 key。
  ai_provider: "none" | "openai-compatible" | "qwen" | "custom";
  ai_base_url: string;
  ai_model: string;
  ai_api_key: string;
  ai_enhance: boolean;
  skip_dongle_check?: boolean;
}

export interface PreflightCheck {
  key: string;
  label: string;
  state: "ok" | "warn" | "missing" | "skip";
  detail: string;
}

export interface Preflight {
  overall: "ready" | "needs_attention" | "blocked";
  checks: PreflightCheck[];
}

export interface PreviewData {
  png_base64: string;
  source_width: number;
  source_height: number;
  preview_width: number;
  preview_height: number;
  scale: number;
}

export interface OverlayData {
  lines: number[][][];
  texts: { left: number; top: number; right: number; bottom: number; text: string }[];
  dropped_lines: number;
}

export interface RunSummary {
  has_report: boolean;
  kind?: "ok" | "warning" | "error";
  message?: string;
  output_root?: string;
  map_id?: string;
  line_candidates?: number | null;
  text_candidates?: number | null;
  area_candidates?: number | null;
  conversion_status?: string | null;
  pixel_unit_raster?: string;
  load_folder?: string;
  ready_files?: string[];
  stage_states?: Record<string, StageState>;
  checked_yes?: number;
}

export interface HistoryRow {
  output_root: string;
  name: string;
  mtime: number;
  kind?: string;
  map_id?: string;
  line_candidates?: number | null;
  text_candidates?: number | null;
  conversion_status?: string | null;
}

export interface BatchRow {
  map_id?: string;
  status?: string;
  line_candidates?: number | string;
  text_candidates?: number | string;
  conversion_status?: string;
  error?: string;
}

export interface LogEntry {
  time: string;
  level: string;
  message: string;
}

export interface MachineSettings {
  [key: string]: string;
}
