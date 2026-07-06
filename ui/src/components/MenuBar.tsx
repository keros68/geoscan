import { useEffect, useRef, useState } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";

interface MenuItem {
  label: string;
  action?: () => void;
  disabled?: boolean;
  separator?: boolean;
}

interface Props {
  busy: boolean;
  onOpenImage: () => void;
  onOpenOutput: () => void;
  onStart: () => void;
  onStop: () => void;
  onPreflight: () => void;
  onAutodetect: () => void;
  onSettings: () => void;
  onAdvanced: () => void;
  onCheckUpdate: () => void;
  onCopyDiagnostics: () => void;
}

export default function MenuBar(props: Props) {
  const [openMenu, setOpenMenu] = useState<string | null>(null);
  const barRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const close = (event: MouseEvent) => {
      if (barRef.current && !barRef.current.contains(event.target as Node)) setOpenMenu(null);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const menus: { key: string; label: string; items: MenuItem[] }[] = [
    {
      key: "file",
      label: "文件",
      items: [
        { label: "打开输入图片…", action: props.onOpenImage, disabled: props.busy },
        { label: "打开输出文件夹", action: props.onOpenOutput },
        { separator: true, label: "" },
        { label: "退出", action: () => void getCurrentWindow().close() },
      ],
    },
    {
      key: "run",
      label: "运行",
      items: [
        { label: "开始运行", action: props.onStart, disabled: props.busy },
        { label: "安全停止", action: props.onStop, disabled: !props.busy },
        { separator: true, label: "" },
        { label: "环境预检", action: props.onPreflight },
      ],
    },
    {
      key: "tools",
      label: "工具",
      items: [
        { label: "高级运行参数…", action: props.onAdvanced },
        { label: "本机程序设置…", action: props.onSettings },
        { label: "复制诊断信息", action: props.onCopyDiagnostics },
        { separator: true, label: "" },
        { label: "检查更新", action: props.onCheckUpdate },
      ],
    },
    {
      key: "help",
      label: "帮助",
      items: [
        {
          label: "关于 GeoScan",
          action: () =>
            window.alert(
              "GeoScan —— 扫描地质图半自动矢量化工具。\n所有候选保持 checked=no，AI 仅复核建议，最终以人工 MapGIS 验收为准。",
            ),
        },
      ],
    },
  ];

  return (
    <div className="menubar" ref={barRef}>
      {menus.map((menu) => (
        <div key={menu.key} className={`menubar-item ${openMenu === menu.key ? "open" : ""}`}>
          <button
            onClick={() => setOpenMenu(openMenu === menu.key ? null : menu.key)}
            onMouseEnter={() => openMenu && setOpenMenu(menu.key)}
          >
            {menu.label}
          </button>
          {openMenu === menu.key && (
            <div className="menu-popup">
              {menu.items.map((item, index) =>
                item.separator ? (
                  <div key={index} className="menu-sep" />
                ) : (
                  <button
                    key={index}
                    disabled={item.disabled}
                    onClick={() => {
                      setOpenMenu(null);
                      item.action?.();
                    }}
                  >
                    {item.label}
                  </button>
                ),
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
