import { useEffect, useState } from "react";
import { open as openDialog } from "@tauri-apps/plugin-dialog";
import { engineCall } from "../engine";

interface Props {
  onClose: () => void;
  onSaved: () => void;
  pushToast: (kind: "ok" | "warning" | "error" | "info", title: string, message: string) => void;
}

interface SettingsForm {
  section_exe: string;
  w60_conv_exe: string;
  dongle_process_name: string;
  ogr2ogr: string;
  gdal_data: string;
  ocr_python: string;
  project_root: string;
}

const EMPTY: SettingsForm = {
  section_exe: "",
  w60_conv_exe: "",
  dongle_process_name: "",
  ogr2ogr: "",
  gdal_data: "",
  ocr_python: "",
  project_root: "",
};

const FIELDS: { key: keyof SettingsForm; label: string; directory?: boolean; hint?: string }[] = [
  { key: "section_exe", label: "SECTION 程序 (section.exe)" },
  { key: "w60_conv_exe", label: "W60 转换程序 (W60_Conv.exe)" },
  {
    key: "dongle_process_name",
    label: "MapGIS 密码狗进程 / exe",
    hint: "默认 dog67.exe；可填进程名，也可选择模拟狗 exe",
  },
  { key: "ogr2ogr", label: "ogr2ogr (QGIS/自带)" },
  { key: "gdal_data", label: "GDAL 数据目录（可选）", directory: true },
  { key: "ocr_python", label: "OCR 解释器（可选）", hint: "留空时文字候选为占位框，属正常" },
  { key: "project_root", label: "默认工作目录", directory: true },
];

export default function SettingsDialog(props: Props) {
  const [form, setForm] = useState<SettingsForm>(EMPTY);
  const [settingsFile, setSettingsFile] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [detecting, setDetecting] = useState(false);

  useEffect(() => {
    void engineCall<any>("get_settings")
      .then((data) => {
        const settings = data.settings ?? {};
        setForm({
          section_exe: settings.section_exe ?? "",
          w60_conv_exe: settings.w60_conv_exe ?? "",
          dongle_process_name: settings.dongle_process_name ?? "",
          ogr2ogr: settings.ogr2ogr ?? "",
          gdal_data: settings.gdal_data ?? "",
          ocr_python: settings.ocr_python ?? "",
          project_root: settings.project_root ?? data.project_root ?? "",
        });
        setSettingsFile(String(data.settings_file ?? ""));
      })
      .catch((error) => props.pushToast("error", "读取设置失败", String(error)))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const pick = async (key: keyof SettingsForm, directory: boolean) => {
    const picked = await openDialog({ directory, title: "选择路径" });
    if (typeof picked === "string" && picked) setForm((prev) => ({ ...prev, [key]: picked }));
  };

  const autodetect = async () => {
    setDetecting(true);
    try {
      const data = await engineCall<{ filled: Record<string, string> }>("autodetect_tools", { settings: form });
      const filled = data.filled ?? {};
      if (Object.keys(filled).length === 0) {
        props.pushToast("warning", "未探测到新路径", "已填写的不会覆盖；请手动选择 MapGIS67 安装目录下的程序。");
      } else {
        setForm((prev) => ({ ...prev, ...filled }));
        props.pushToast("ok", "探测完成", Object.entries(filled).map(([k, v]) => `${k}: ${v}`).join("\n"));
      }
    } catch (error) {
      props.pushToast("error", "探测失败", String(error));
    } finally {
      setDetecting(false);
    }
  };

  const save = async () => {
    setSaving(true);
    try {
      await engineCall("save_settings", { settings: form });
      props.onSaved();
      props.onClose();
    } catch (error) {
      props.pushToast("error", "保存失败", String(error instanceof Error ? error.message : error));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-mask" onMouseDown={(event) => event.target === event.currentTarget && props.onClose()}>
      <div className="modal wide">
        <div className="modal-header">
          本机程序设置
          <button onClick={props.onClose} aria-label="关闭">
            ✕
          </button>
        </div>
        <div className="modal-body">
          <p className="hint" style={{ marginBottom: 12 }}>
            每台电脑的 MapGIS/QGIS 安装目录不同；保存后立即生效，下次启动自动加载。设置文件不保存任何 API Key。
          </p>
          <div className="form-rows">
            {FIELDS.map((field) => (
              <FieldRow
                key={field.key}
                label={field.label}
                value={form[field.key]}
                hint={field.hint}
                disabled={loading}
                onChange={(value) => setForm((prev) => ({ ...prev, [field.key]: value }))}
                onPick={() => void pick(field.key, Boolean(field.directory))}
              />
            ))}
            <label>设置文件</label>
            <input type="text" className="full" value={settingsFile} readOnly style={{ color: "var(--ink-muted)" }} />
          </div>
        </div>
        <div className="modal-footer">
          <button className="btn" onClick={autodetect} disabled={detecting || loading}>
            {detecting ? "正在探测…" : "自动探测本机程序"}
          </button>
          <span style={{ flex: 1 }} />
          <button className="btn" onClick={props.onClose}>
            取消
          </button>
          <button className="btn primary" onClick={save} disabled={saving || loading}>
            {saving ? "保存中…" : "保存本机设置"}
          </button>
        </div>
      </div>
    </div>
  );
}

function FieldRow(props: {
  label: string;
  value: string;
  hint?: string;
  disabled: boolean;
  onChange: (value: string) => void;
  onPick: () => void;
}) {
  return (
    <>
      <label>{props.label}</label>
      <input
        type="text"
        value={props.value}
        disabled={props.disabled}
        onChange={(event) => props.onChange(event.target.value)}
      />
      <button className="btn" onClick={props.onPick} disabled={props.disabled}>
        选择
      </button>
      {props.hint && <div className="form-hint">{props.hint}</div>}
    </>
  );
}
