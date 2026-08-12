import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

const WIDTH = 2400;
const HEIGHT = 1500;
const elements = [];
const svg = [];
let serial = 0;

const colors = {
  title: "#1e40af",
  text: "#374151",
  muted: "#6b7280",
  line: "#64748b",
  blue: "#2563eb",
  blueFill: "#a5d8ff",
  purple: "#7c3aed",
  purpleFill: "#d0bfff",
  teal: "#0f766e",
  tealFill: "#c3fae8",
  orange: "#c2410c",
  orangeFill: "#ffd8a8",
  green: "#15803d",
  greenFill: "#b2f2bb",
  red: "#b91c1c",
  redFill: "#ffc9c9",
  yellow: "#a16207",
  yellowFill: "#fff3bf",
  laneBlue: "#dbe4ff",
  lanePurple: "#e5dbff",
  laneGreen: "#d3f9d8",
  laneYellow: "#fff9db",
  white: "#ffffff",
};

function id(prefix) {
  serial += 1;
  return `${prefix}-${serial}`;
}

function seed() {
  return 100000 + serial * 7919;
}

function common(elementId, type, x, y, width, height, style = {}) {
  return {
    id: elementId,
    type,
    x,
    y,
    width,
    height,
    angle: 0,
    strokeColor: style.strokeColor ?? colors.text,
    backgroundColor: style.backgroundColor ?? "transparent",
    fillStyle: "solid",
    strokeWidth: style.strokeWidth ?? 2,
    strokeStyle: style.strokeStyle ?? "solid",
    roughness: style.roughness ?? 1,
    opacity: style.opacity ?? 100,
    groupIds: [],
    roundness: type === "rectangle" ? { type: 3 } : type === "arrow" ? { type: 2 } : null,
    seed: seed(),
    version: 1,
    isDeleted: false,
    boundElements: null,
    updated: 1,
    link: null,
    locked: false,
  };
}

function escapeXml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function lineMetrics(value, fontSize) {
  let units = 0;
  for (const char of value) units += char.charCodeAt(0) > 255 ? 1 : 0.56;
  return Math.max(fontSize, units * fontSize);
}

function textElement(label, x, y, options = {}) {
  const fontSize = options.fontSize ?? 18;
  const lines = label.split("\n");
  const width = Math.max(...lines.map((line) => lineMetrics(line, fontSize)));
  const height = lines.length * fontSize * 1.25;
  const elementId = id("text");
  const align = options.align ?? "center";
  const textX = align === "left" ? x : x - width / 2;
  const textY = y - height / 2;
  elements.push({
    ...common(elementId, "text", textX, textY, width, height, {
      strokeColor: options.color ?? colors.text,
      strokeWidth: 1,
      roughness: 0,
      opacity: options.opacity ?? 100,
    }),
    text: label,
    fontSize,
    fontFamily: 5,
    textAlign: align,
    verticalAlign: "middle",
    containerId: null,
    originalText: label,
    autoResize: true,
    lineHeight: 1.25,
  });

  const anchor = align === "left" ? "start" : "middle";
  const svgX = align === "left" ? x : x;
  const svgLineHeight = fontSize * 1.25;
  const firstLineY = y - ((lines.length - 1) * svgLineHeight) / 2;
  lines.forEach((line, index) => {
    svg.push(`<text x="${svgX}" y="${firstLineY + index * svgLineHeight}" text-anchor="${anchor}" dominant-baseline="middle" fill="${options.color ?? colors.text}" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif" font-size="${fontSize}" font-weight="${options.weight ?? 500}">${escapeXml(line)}</text>`);
  });
}

function rectangle(x, y, width, height, style = {}) {
  const elementId = id("rect");
  elements.push(common(elementId, "rectangle", x, y, width, height, style));
  svg.push(`<rect x="${x}" y="${y}" width="${width}" height="${height}" rx="${style.radius ?? 8}" fill="${style.backgroundColor ?? "none"}" fill-opacity="${(style.opacity ?? 100) / 100}" stroke="${style.strokeColor ?? colors.text}" stroke-width="${style.strokeWidth ?? 2}" ${style.strokeStyle === "dashed" ? 'stroke-dasharray="10 8"' : ""}/>`);
}

function node(x, y, width, height, label, style = {}) {
  rectangle(x, y, width, height, style);
  textElement(label, x + width / 2, y + height / 2, {
    fontSize: style.fontSize ?? 18,
    color: style.textColor ?? colors.text,
    weight: style.weight ?? 600,
  });
}

function diamond(x, y, width, height, label, style = {}) {
  const elementId = id("diamond");
  elements.push({
    ...common(elementId, "diamond", x, y, width, height, style),
    roundness: { type: 2 },
  });
  svg.push(`<polygon points="${x + width / 2},${y} ${x + width},${y + height / 2} ${x + width / 2},${y + height} ${x},${y + height / 2}" fill="${style.backgroundColor ?? "none"}" stroke="${style.strokeColor ?? colors.text}" stroke-width="${style.strokeWidth ?? 2}"/>`);
  textElement(label, x + width / 2, y + height / 2, {
    fontSize: style.fontSize ?? 17,
    color: style.textColor ?? colors.text,
    weight: 650,
  });
}

function arrow(points, options = {}) {
  const elementId = id("arrow");
  const [startX, startY] = points[0];
  const relative = points.map(([x, y]) => [x - startX, y - startY]);
  const xs = points.map(([x]) => x);
  const ys = points.map(([, y]) => y);
  elements.push({
    ...common(
      elementId,
      "arrow",
      startX,
      startY,
      Math.max(...xs) - Math.min(...xs),
      Math.max(...ys) - Math.min(...ys),
      {
        strokeColor: options.color ?? colors.line,
        strokeWidth: options.width ?? 2,
        strokeStyle: options.dashed ? "dashed" : "solid",
        roughness: 1,
      },
    ),
    points: relative,
    lastCommittedPoint: null,
    startBinding: null,
    endBinding: null,
    startArrowhead: options.startArrowhead ?? null,
    endArrowhead: options.endArrowhead === undefined ? "arrow" : options.endArrowhead,
  });
  const svgPoints = points.map(([x, y]) => `${x},${y}`).join(" ");
  svg.push(`<polyline points="${svgPoints}" fill="none" stroke="${options.color ?? colors.line}" stroke-width="${options.width ?? 2}" stroke-linecap="round" stroke-linejoin="round" ${options.dashed ? 'stroke-dasharray="10 8"' : ""} marker-end="url(#arrow-${(options.color ?? colors.line).slice(1)})"/>`);
  if (options.label) {
    const [labelX, labelY] = options.labelAt ?? points[Math.floor(points.length / 2)];
    textElement(options.label, labelX, labelY, {
      fontSize: options.labelSize ?? 15,
      color: options.color ?? colors.text,
      weight: 650,
    });
  }
}

function lane(y, height, title, subtitle, fill, accent) {
  rectangle(40, y, 2320, height, {
    backgroundColor: fill,
    strokeColor: accent,
    strokeWidth: 1,
    opacity: 30,
    radius: 6,
  });
  rectangle(65, y + 25, 185, 76, {
    backgroundColor: colors.white,
    strokeColor: accent,
    strokeWidth: 2,
    opacity: 92,
    radius: 6,
  });
  textElement(title, 158, y + 48, { fontSize: 20, color: accent, weight: 700 });
  textElement(subtitle, 158, y + 79, { fontSize: 14, color: colors.muted, weight: 500 });
}

// Background and heading.
svg.push(`<rect width="${WIDTH}" height="${HEIGHT}" fill="#ffffff"/>`);
textElement("DeepSee 当前应用全流程", 70, 54, {
  fontSize: 30,
  color: colors.title,
  weight: 750,
  align: "left",
});
textElement("产品主线 + 工程实现旁路｜当前 Web 与安全多模态网关", 70, 94, {
  fontSize: 17,
  color: colors.muted,
  weight: 500,
  align: "left",
});
node(1840, 28, 490, 70, "现状基线  Desktop 7586ffd  ·  Gateway ff80bdd", {
  backgroundColor: colors.yellowFill,
  strokeColor: colors.yellow,
  textColor: colors.yellow,
  fontSize: 16,
  strokeWidth: 2,
});

lane(130, 210, "用户与 Web", "产品入口", colors.laneBlue, colors.blue);
lane(360, 250, "本地网关", "协议与安全", colors.lanePurple, colors.purple);
lane(630, 440, "视觉与推理", "组合管线", colors.laneGreen, colors.teal);
lane(1090, 360, "响应与观测", "返回和旁路", colors.laneYellow, colors.green);

// User and Web lane.
node(300, 185, 220, 110, "用户\nChat / Playground", {
  backgroundColor: colors.blueFill,
  strokeColor: colors.blue,
  textColor: colors.title,
});
node(620, 175, 310, 130, "React Web\n文本 + 单张图片\n模型 + 视觉模式", {
  backgroundColor: colors.blueFill,
  strokeColor: colors.blue,
  textColor: colors.title,
});
node(1040, 175, 340, 130, "HttpDesktopBridge\n完整对话历史 + Data URL\nAbortController", {
  backgroundColor: colors.blueFill,
  strokeColor: colors.blue,
  textColor: colors.title,
});
node(1490, 175, 420, 130, "POST /v1/chat/completions\nBearer public key · stream=true\nInclude-Vision + Vision-Mode", {
  backgroundColor: colors.purpleFill,
  strokeColor: colors.purple,
  textColor: "#5b21b6",
  fontSize: 16,
});
node(2020, 185, 280, 110, "OpenAI 兼容客户端\n也可直连同一端点", {
  backgroundColor: colors.white,
  strokeColor: colors.blue,
  textColor: colors.title,
  strokeStyle: "dashed",
  fontSize: 16,
});
arrow([[520, 240], [620, 240]], { color: colors.blue, width: 3, label: "输入", labelAt: [570, 218] });
arrow([[930, 240], [1040, 240]], { color: colors.blue, width: 3, label: "组装消息", labelAt: [985, 216] });
arrow([[1380, 240], [1490, 240]], { color: colors.blue, width: 3, label: "fetch", labelAt: [1435, 217] });

// Gateway lane.
node(300, 425, 260, 120, "FastAPI 网关\n同源静态托管 /\n/health 免鉴权", {
  backgroundColor: colors.purpleFill,
  strokeColor: colors.purple,
  textColor: "#5b21b6",
  fontSize: 16,
});
node(650, 415, 290, 140, "入口守卫\nCORS + public key\n32 MiB 请求体上限\n生成 trace ID", {
  backgroundColor: colors.purpleFill,
  strokeColor: colors.purple,
  textColor: "#5b21b6",
  fontSize: 16,
});
node(1030, 415, 300, 140, "协议解析与校验\nOpenAI 消息形状\n受支持参数 · 内容类型\n配置后置加载", {
  backgroundColor: colors.purpleFill,
  strokeColor: colors.purple,
  textColor: "#5b21b6",
  fontSize: 16,
});
diamond(1420, 425, 200, 120, "消息中\n有图片？", {
  backgroundColor: colors.yellowFill,
  strokeColor: colors.yellow,
  textColor: colors.yellow,
  fontSize: 17,
});
node(1740, 430, 260, 110, "纯文本路径\n保留完整消息与工具字段", {
  backgroundColor: colors.white,
  strokeColor: colors.blue,
  textColor: colors.title,
  fontSize: 16,
});
node(2080, 425, 240, 120, "管理旁路\n/admin/traces\nadmin key", {
  backgroundColor: colors.white,
  strokeColor: colors.purple,
  textColor: "#5b21b6",
  strokeStyle: "dashed",
  fontSize: 16,
});
arrow([[1700, 305], [1700, 390], [430, 390], [430, 425]], { color: colors.purple, width: 3 });
arrow([[2160, 295], [2160, 390], [430, 390], [430, 425]], { color: colors.blue, width: 2, dashed: true, label: "直连", labelAt: [2108, 375] });
arrow([[560, 485], [650, 485]], { color: colors.purple, width: 3 });
arrow([[940, 485], [1030, 485]], { color: colors.purple, width: 3 });
arrow([[1330, 485], [1420, 485]], { color: colors.purple, width: 3 });
arrow([[1620, 485], [1740, 485]], { color: colors.blue, width: 3, label: "否", labelAt: [1680, 463] });

// Vision and reasoning lane.
node(300, 700, 270, 120, "图片归一化\nData URL / HTTP URL\n逐张处理", {
  backgroundColor: colors.tealFill,
  strokeColor: colors.teal,
  textColor: colors.teal,
  fontSize: 16,
});
node(650, 690, 300, 140, "图片安全管线\nSSRF + 每跳重定向校验\n20 MiB · 格式 · 16.7 MP\n固定已验证 IP", {
  backgroundColor: colors.tealFill,
  strokeColor: colors.teal,
  textColor: colors.teal,
  fontSize: 16,
});
diamond(1030, 700, 230, 120, "视觉路由\nauto / ui / general", {
  backgroundColor: colors.yellowFill,
  strokeColor: colors.yellow,
  textColor: colors.yellow,
  fontSize: 16,
});
diamond(1350, 700, 220, 120, "进程内缓存\n命中？", {
  backgroundColor: colors.tealFill,
  strokeColor: colors.teal,
  textColor: colors.teal,
  fontSize: 17,
});
node(1690, 670, 330, 150, "可插拔视觉后端\nOpenAI-compatible\nAnthropic / Gemini\n生成 UI 地图或通用描述", {
  backgroundColor: colors.orangeFill,
  strokeColor: colors.orange,
  textColor: colors.orange,
  fontSize: 16,
});
node(1690, 895, 330, 105, "写入视觉缓存\n键包含图片 · 问题 · 模式 · 配置", {
  backgroundColor: colors.tealFill,
  strokeColor: colors.teal,
  textColor: colors.teal,
  fontSize: 16,
});
node(1320, 895, 270, 105, "视觉分析注入\n替换图片内容并保留历史", {
  backgroundColor: colors.tealFill,
  strokeColor: colors.teal,
  textColor: colors.teal,
  fontSize: 16,
});
node(890, 885, 300, 125, "DeepSeek API\nChat Completions\n文本 · 工具调用 · 流式", {
  backgroundColor: colors.orangeFill,
  strokeColor: colors.orange,
  textColor: colors.orange,
  fontSize: 17,
});
arrow([[1520, 545], [1520, 640], [435, 640], [435, 700]], { color: colors.teal, width: 3, label: "是", labelAt: [1486, 625] });
arrow([[570, 760], [650, 760]], { color: colors.teal, width: 3 });
arrow([[950, 760], [1030, 760]], { color: colors.teal, width: 3 });
arrow([[1260, 760], [1350, 760]], { color: colors.teal, width: 3 });
arrow([[1570, 760], [1690, 760]], { color: colors.orange, width: 3, label: "未命中", labelAt: [1630, 737] });
arrow([[1855, 820], [1855, 895]], { color: colors.orange, width: 3, label: "分析结果", labelAt: [1912, 860] });
arrow([[1690, 948], [1590, 948]], { color: colors.teal, width: 3 });
arrow([[1460, 820], [1460, 895]], { color: colors.teal, width: 3, label: "命中复用", labelAt: [1510, 856] });
arrow([[1320, 948], [1190, 948]], { color: colors.teal, width: 3, label: "统一消息历史", labelAt: [1255, 925] });
arrow([[1870, 540], [2300, 540], [2300, 1040], [1040, 1040], [1040, 1010]], { color: colors.blue, width: 3, label: "纯文本直通", labelAt: [2190, 1020] });

// Response and observability lane.
node(300, 1150, 350, 130, "请求追踪完成\n状态 · 延迟 · 路由 · 图片数\n缓存命中 · 上游模型 · 错误", {
  backgroundColor: colors.white,
  strokeColor: colors.purple,
  textColor: "#5b21b6",
  strokeStyle: "dashed",
  fontSize: 16,
});
node(760, 1140, 340, 150, "网关响应编码\nvision_analysis 先发\ncontent / tool_calls 增量\nfinish_reason → [DONE]", {
  backgroundColor: colors.greenFill,
  strokeColor: colors.green,
  textColor: colors.green,
  fontSize: 16,
});
node(1200, 1150, 330, 130, "Bridge 解析 SSE\nstart · analysis · chunk\ntool_calls · complete", {
  backgroundColor: colors.greenFill,
  strokeColor: colors.green,
  textColor: colors.green,
  fontSize: 16,
});
node(1630, 1150, 300, 130, "React 实时渲染\n回答逐步更新\n视觉分析可展开", {
  backgroundColor: colors.greenFill,
  strokeColor: colors.green,
  textColor: colors.green,
  fontSize: 16,
});
node(2030, 1160, 270, 110, "用户获得回复\n可停止 · 重试 · 复制", {
  backgroundColor: colors.greenFill,
  strokeColor: colors.green,
  textColor: colors.green,
  fontSize: 17,
});
arrow([[1040, 1010], [1040, 1080], [930, 1080], [930, 1140]], { color: colors.green, width: 3, label: "completion / SSE", labelAt: [1110, 1062] });
arrow([[1100, 1215], [1200, 1215]], { color: colors.green, width: 3 });
arrow([[1530, 1215], [1630, 1215]], { color: colors.green, width: 3 });
arrow([[1930, 1215], [2030, 1215]], { color: colors.green, width: 3 });
arrow([[795, 555], [795, 600], [600, 600], [600, 1120], [475, 1120], [475, 1150]], { color: colors.purple, width: 2, dashed: true, label: "响应结束后落 trace", labelAt: [620, 1103] });
arrow([[760, 1215], [650, 1215]], { color: colors.purple, width: 2, dashed: true });
arrow([[2200, 545], [2200, 1080], [580, 1080], [580, 1150]], { color: colors.purple, width: 2, dashed: true, label: "GET /admin/traces", labelAt: [2110, 1060] });

node(300, 1330, 470, 90, "错误映射：400 / 401 / 413 / 502 / 503", {
  backgroundColor: colors.redFill,
  strokeColor: colors.red,
  textColor: colors.red,
  strokeStyle: "dashed",
  fontSize: 16,
});
node(900, 1330, 420, 90, "流式上游失败 → SSE error chunk", {
  backgroundColor: colors.redFill,
  strokeColor: colors.red,
  textColor: colors.red,
  strokeStyle: "dashed",
  fontSize: 16,
});
node(1450, 1330, 430, 90, "用户停止 → AbortController → 取消 fetch", {
  backgroundColor: colors.redFill,
  strokeColor: colors.red,
  textColor: colors.red,
  strokeStyle: "dashed",
  fontSize: 16,
});
node(1990, 1330, 310, 90, "隐私：trace 仅保存\n视觉字符数 + 摘要指纹", {
  backgroundColor: colors.yellowFill,
  strokeColor: colors.yellow,
  textColor: colors.yellow,
  strokeStyle: "dashed",
  fontSize: 15,
});
arrow([[1180, 555], [1180, 615], [275, 615], [275, 1375], [300, 1375]], { color: colors.red, width: 2, dashed: true });
arrow([[930, 1290], [930, 1330]], { color: colors.red, width: 2, dashed: true });
arrow([[2165, 1270], [2165, 1305], [1665, 1305], [1665, 1330]], { color: colors.red, width: 2, dashed: true });

// Small current-scope callout.
node(70, 1460, 2260, 28, "当前实现边界：Web + 本地安全网关；Tauri 进程托管、Codewhale 执行、跨重启会话持久化未进入本图", {
  backgroundColor: colors.white,
  strokeColor: colors.white,
  textColor: colors.muted,
  fontSize: 14,
  strokeWidth: 0,
});

const standard = {
  type: "excalidraw",
  version: 2,
  source: "https://excalidraw.com",
  elements,
  appState: { gridSize: null, viewBackgroundColor: "#ffffff" },
  files: {},
};

const obsidian = {
  ...standard,
  source: "https://github.com/zsviczian/obsidian-excalidraw-plugin",
};

const base = "DeepSee当前应用全流程.flowchart";
const outputDir = process.cwd();
fs.writeFileSync(path.join(outputDir, `${base}.excalidraw`), `${JSON.stringify(standard, null, 2)}\n`);
const markdown = `---
excalidraw-plugin: parsed
tags: [excalidraw]
---
==⚠  Switch to EXCALIDRAW VIEW in the MORE OPTIONS menu of this document. ⚠== You can decompress Drawing data with the command palette: 'Decompress current Excalidraw file'. For more info check in plugin settings under 'Saving'

# Excalidraw Data

## Text Elements
%%
## Drawing
\`\`\`json
${JSON.stringify(obsidian, null, 2)}
\`\`\`
%%
`;
fs.writeFileSync(path.join(outputDir, `${base}.md`), markdown);

const markerColors = [...new Set(elements.filter((element) => element.type === "arrow").map((element) => element.strokeColor))];
const markers = markerColors.map((color) => `<marker id="arrow-${color.slice(1)}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="${color}"/></marker>`).join("");
const svgDocument = `<svg xmlns="http://www.w3.org/2000/svg" width="${WIDTH}" height="${HEIGHT}" viewBox="0 0 ${WIDTH} ${HEIGHT}"><defs>${markers}<filter id="soft-shadow" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#0f172a" flood-opacity="0.08"/></filter></defs>${svg.join("")}</svg>\n`;
fs.writeFileSync(path.join(outputDir, `${base}.svg`), svgDocument);

const pngResult = spawnSync(
  "sips",
  ["-s", "format", "png", `${base}.svg`, "--out", `${base}.png`],
  { cwd: outputDir, encoding: "utf8" },
);
if (pngResult.status !== 0) {
  throw new Error(`PNG rendering failed: ${pngResult.stderr || pngResult.stdout}`);
}

console.log(`Generated ${elements.length} Excalidraw elements in ${outputDir}`);
