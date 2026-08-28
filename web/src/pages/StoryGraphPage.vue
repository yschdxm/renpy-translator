<script setup lang="ts">
/** 剧情分支图：场景级故事图（ECharts graph series layout:'none' + 离线
 *  dagre 坐标，原生 roam 平移缩放——坐标/符号/连线/展开点在同一视图里
 *  整体缩放，不存在分离问题；展开点 = 同系列的圆形节点。
 *  标签字号不随 roam 缩放是 echarts 固有行为，这里按 zoom 手动补偿）
 *  默认全部收起，点卡片右侧 + 逐级展开分支 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  NButton, NCard, NDrawer, NDrawerContent, NEmpty, NInput, NProgress,
  NSelect, NSpace, NSpin, NStatistic, NSwitch, NTag, NText, useMessage,
} from 'naive-ui'
import { PlayOutline, RefreshOutline } from '@vicons/ionicons5'
import * as echarts from 'echarts'
import { DagreLayout } from '@antv/layout'

import { api, toastError } from '../api/client'
import { renderIcon } from '../components/icons'
import { GT } from '../components/graph-theme'
import { MiniMap, makeChart } from '../components/echarts-utils'
import { useJobsStore } from '../stores/jobs'

interface ApiScene {
  scene_id: string
  title: string
  summary: string
  labels_json: string
  speakers_json: string
  first_text: string
  first_text_cn: string
  dialogue_count: number
  translated_count: number
  tl_total: number
  thumb_file: string
  is_entry: number
  is_ending: number
  is_return: number
  file_path: string
  line_start: number
}
interface ApiEdge {
  id: number
  source: string
  target: string
  texts_json: string
  texts_cn_json: string
  branch: string
  has_call: number
  unresolved: number
}
interface CharInfo { name: string; original: string; avatar: boolean }
interface SceneData {
  scenes: ApiScene[]
  edges: ApiEdge[]
  stats: { built_at: string }
  characters: Record<string, CharInfo>
}
interface DialogueLine {
  file_path: string
  line_number: number
  character: string
  character_cn: string
  original_text: string
  translated_text: string
  is_translated: number
}

interface NodeItem {
  id: string
  x: number
  y: number
  h: number
  title: string
  summary: string
  thumbUrl: string
  progress: number
  dialogueCount: number
  speakers: string[]
  isEntry: boolean
  isEnding: boolean
  expandCount: number
  expanded: boolean
  unresolved: boolean
}

const UNRESOLVED_ID = '__unresolved__'
const CARD_W = 202
const CARD_H_THUMB = 196
const CARD_H_PLAIN = 88
/** 缩略图画幅（数据单位）：与 graph_cache 960x540 同比例 */
const THUMB_W = 202
const THUMB_H = 111
/** 展开圆点距卡片右缘的间距（数据单位） */
const DOT_GAP = 16

const message = useMessage()
const jobsStore = useJobsStore()

const loading = ref(true)
const data = ref<SceneData | null>(null)
const searchInput = ref('')
const search = ref('')
const fileFilter = ref<string | null>(null)
const showUnresolved = ref(true)
/** 已展开的场景 id 集合（默认全收起，只有入口可见） */
const expanded = ref(new Set<string>())
/** 展开操作后要聚焦的节点 */
let focusAfterRender: string | null = null

let searchTimer: ReturnType<typeof setTimeout> | undefined
watch(searchInput, (v) => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => { search.value = v }, 300)
})

async function load() {
  loading.value = true
  try {
    data.value = await api.get<SceneData>('/api/current/graph/scenes')
  } catch (e) {
    toastError(message, e)
  } finally {
    loading.value = false
  }
}

async function build() {
  try {
    const d = await api.post<{ job_id: string }>(
      '/api/current/graph/story/build')
    jobsStore.track(d.job_id)
  } catch (e) {
    toastError(message, e)
  }
}

watch(
  () => [...jobsStore.jobs.values()]
    .filter((j) => j.kind === 'graph.story-build')
    .map((j) => `${j.id}:${j.status}`).join(','),
  () => { load() },
)

const hasGraph = computed(() => (data.value?.scenes.length ?? 0) > 0)

const fileOptions = computed(() => {
  const files = new Set<string>()
  for (const s of data.value?.scenes ?? []) files.add(s.file_path)
  return [...files].sort().map((f) => ({ label: f, value: f }))
})

const endings = computed(() =>
  (data.value?.scenes ?? []).filter((s) => s.is_ending))

const endingOptions = computed(() =>
  endings.value.map((s) => ({
    label: `${s.title || s.scene_id}（${s.dialogue_count} 句）`,
    value: s.scene_id,
  })))

function parseJson(s: string): string[] {
  try { return JSON.parse(s || '[]') } catch { return [] }
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n)}…` : s
}

function speakersOf(s: ApiScene): string[] {
  return parseJson(s.speakers_json)
    .map((v) => data.value?.characters[v]?.name ?? v)
}

function progressOf(s: ApiScene): number {
  if (!s.tl_total) return -1
  return Math.round(100 * s.translated_count / s.tl_total)
}

/** 剥掉 Ren'Py 文本标记（[color=…]/{w} 等），避免漏进图上的标签 */
function stripRenpyTags(s: string): string {
  return s.replace(/\[[^\]]*\]|\{[^}]*\}/g, '').trim()
}

function edgeLabel(e: ApiEdge): string {
  const textsCn = parseJson(e.texts_cn_json)
  const texts = parseJson(e.texts_json)
  return texts.map((t, i) => stripRenpyTags(textsCn[i] || t))
    .filter(Boolean).join(' / ')
}

function edgeColor(e: ApiEdge): string {
  if (e.unresolved) return GT.error
  if (e.branch === 'menu') return GT.info
  if (e.branch === 'condition') return GT.warning
  if (e.has_call) return GT.muted
  return GT.border
}

const outMap = computed(() => {
  const m = new Map<string, ApiEdge[]>()
  for (const e of data.value?.edges ?? []) {
    const list = m.get(e.source) ?? []
    list.push(e)
    m.set(e.source, list)
  }
  return m
})
const sceneById = computed(() =>
  new Map((data.value?.scenes ?? []).map((s) => [s.scene_id, s])))

/** 从入口出发的最短路径树（BFS），用于搜索命中时展开必须的祖先链 */
const shortestParent = computed(() => {
  const parent = new Map<string, string>()
  if (!data.value) return parent
  const entry = data.value.scenes.find((s) => s.is_entry)
  if (!entry) return parent
  const queue = [entry.scene_id]
  const seen = new Set([entry.scene_id])
  while (queue.length) {
    const cur = queue.shift()!
    for (const e of outMap.value.get(cur) ?? []) {
      if (e.unresolved || !sceneById.value.has(e.target)) continue
      if (!seen.has(e.target)) {
        seen.add(e.target)
        parent.set(e.target, cur)
        queue.push(e.target)
      }
    }
  }
  return parent
})

/** 搜索/过滤命中的场景集合（按文件过滤 = 按标题/摘要内容匹配） */
const hitIds = computed(() => {
  const hits = new Set<string>()
  if (!data.value) return hits
  const q = search.value.trim().toLowerCase()
  for (const s of data.value.scenes) {
    if (q) {
      const hay = `${s.scene_id} ${s.title} ${s.summary}`.toLowerCase()
      if (!hay.includes(q)) continue
    }
    if (fileFilter.value && s.file_path !== fileFilter.value) continue
    hits.add(s.scene_id)
  }
  return hits
})

/** 收起语义下的可见场景集合：
 *  - 搜索命中：命中节点 + 各自到入口的最短路径上的祖先全部展开
 *  - 按文件过滤：不自动展开，只筛"当前已展开"语义下可见的节点
 *  - 无搜索/过滤：正常展开语义 */
const visibleIds = computed(() => {
  const visible = new Set<string>()
  if (!data.value) return visible
  const q = search.value.trim()
  const entry = data.value.scenes.find((s) => s.is_entry)

  if (q) {
    // 搜索：命中的 + 路径祖先（最短路径树回溯），全部标记为已展开
    const hits = hitIds.value
    for (const h of hits) {
      visible.add(h)
      let cur = h
      while (shortestParent.value.has(cur)) {
        cur = shortestParent.value.get(cur)!
        visible.add(cur)
      }
    }
    return visible
  }

  if (fileFilter.value) {
    // 按文件过滤：只筛，不展开（当前展开语义下的可见节点中筛选）
    const base = new Set<string>()
    if (!entry) {
      for (const s of data.value.scenes) base.add(s.scene_id)
    } else {
      const queue = [entry.scene_id]
      base.add(entry.scene_id)
      while (queue.length) {
        const cur = queue.shift()!
        if (!expanded.value.has(cur)) continue
        for (const e of outMap.value.get(cur) ?? []) {
          if (!e.unresolved && !base.has(e.target)
              && sceneById.value.has(e.target)) {
            base.add(e.target)
            queue.push(e.target)
          }
        }
      }
    }
    for (const id of base) {
      if (data.value.scenes.find(
          (s) => s.scene_id === id && s.file_path === fileFilter.value)) {
        visible.add(id)
      }
    }
    return visible
  }

  // 无搜索/过滤：正常展开语义
  if (!entry) {
    for (const s of data.value.scenes) visible.add(s.scene_id)
    return visible
  }
  const queue = [entry.scene_id]
  visible.add(entry.scene_id)
  while (queue.length) {
    const cur = queue.shift()!
    if (!expanded.value.has(cur)) continue
    for (const e of outMap.value.get(cur) ?? []) {
      if (!e.unresolved && !visible.has(e.target)
          && sceneById.value.has(e.target)) {
        visible.add(e.target)
        queue.push(e.target)
      }
    }
  }
  return visible
})

// ---- ECharts 实例管理 ----
const container = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null
let minimap: MiniMap | null = null
const minimapEl = ref<HTMLElement | null>(null)
let nodeList: NodeItem[] = []
let linkList: any[] = []
/** 边标签的隐形节点（symbol none，坐标 = 边中点，随系列 roam 跟随） */
let edgeLabelNodes: any[] = []
let firstRender = true
let animating = false
/** echarts graph 视图把内容包围盒 fit 进容器（rawTrans），roam zoom 乘在
 *  上面：位置缩放 = fitScale × zoom，符号缩放 = zoom（nodeScaleRatio=1）。
 *  符号尺寸必须乘以 fitScale 才能与 dagre 间距保持一致——fitScale 只在
 *  数据/容器变化时校准一次，zoom 过程纯变换零开销 */
let fitScale = 1
/** 初始 fit 时的有效缩放（标签字号基准：fontSize = 基准 × 有效缩放/fitBase，
 *  与节点尺寸严格同比——节点像素 = 单位 × 有效缩放） */
let fitBase = 1

/** 当前 roam 缩放（series.zoom；首次 setOption 前 getOption() 为
 *  undefined，可选链必须覆盖到 getOption() 本身） */
function currentZoom(): number {
  const z = (chart?.getOption?.()?.series as any[])?.[0]?.zoom
  return typeof z === 'number' && z > 0 ? z : 1
}

/** 有效缩放（1 数据单位 = 多少 px，含 fit × zoom） */
function effectiveScale(): number {
  if (!chart) return 1
  try {
    const p0 = chart.convertToPixel({ seriesIndex: 0 }, [0, 0])
    const p1 = chart.convertToPixel({ seriesIndex: 0 }, [1, 0])
    if (!p0 || !p1) return 1
    return Math.abs(p1[0] - p0[0])
  } catch {
    return 1
  }
}

/** 标签字号补偿的 rAF 句柄（roam 时 echarts 不缩放文字，手动按有效缩放补；
 *  rAF 逐帧同步替代节流——滚轮连发自动合并，缩放过程标签不滞后。
 *  注意不能用 series labelLayout 回调做这件事：它会让标签脱离附着
 *  模式，且边标签根本不进 LabelManager 列表（pan 时钉死在旧位置） */
let labelRaf = 0

/** 上次应用的字号（变化 <3% 不重复 setOption） */
let lastFontSize = 0

/** 标签字号随有效缩放（datum 不设 fontSize，全部继承 series 级
 *  label/edgeLabel；基准 = 初始 fit 时的字号，之后与节点严格同比） */
function applyLabelZoom() {
  if (!chart) return
  const s = effectiveScale() / fitBase
  const fs = Math.max(6, 12 * s)
  if (Math.abs(fs - lastFontSize) / Math.max(lastFontSize, 1e-6) < 0.03) return
  lastFontSize = fs
  chart.setOption({
    series: [{
      label: { fontSize: fs },
      edgeLabel: { fontSize: Math.max(6, 9 * s) },
    }],
  })
}

function scheduleLabelZoom() {
  if (labelRaf) return
  labelRaf = requestAnimationFrame(() => {
    labelRaf = 0
    applyLabelZoom()
  })
}

/** 校准 fitScale（数据/容器变化后）：符号尺寸补偿 */
function calibrateSizes() {
  if (!chart || !nodeList.length) return
  const f = effectiveScale() / currentZoom()
  if (Math.abs(f - fitScale) / Math.max(fitScale, 1e-6) > 0.03) {
    fitScale = f
    chart.setOption({
      series: [{
        data: [
          ...nodeList.map((n) => nodeDatum(n, fitScale)),
          ...nodeList.filter((n) => n.expandCount > 0)
            .map((n) => dotDatum(n, fitScale)),
          ...edgeLabelNodes,
        ],
      }],
    })
  }
}

/** 节点 datum（symbolSize × fitScale 后与 dagre 间距严格一致，
 *  roam zoom 时 echarts 原生按比例整体缩放，连线永不分离） */
function nodeDatum(n: NodeItem, f: number): any {
  const borderColor = n.unresolved ? GT.error
    : n.isEntry ? GT.primary
    : n.isEnding ? '#b8860b'
    : n.progress >= 100 ? GT.primary
    : n.progress > 0 ? GT.warning : GT.border
  return {
    id: n.id,
    name: n.unresolved ? '未决跳转' : n.title,
    x: n.x,
    y: n.y,
    symbol: n.thumbUrl ? `image://${n.thumbUrl}` : 'roundRect',
    symbolSize: n.thumbUrl
      ? [THUMB_W * f, THUMB_H * f]
      : [CARD_W * f, (n.unresolved ? 40 : CARD_H_PLAIN) * 0.7 * f],
    itemStyle: {
      color: n.thumbUrl ? undefined : GT.card,
      borderColor,
      borderWidth: (n.isEntry || n.isEnding ? 2.5 : 1.5) * Math.max(f, 0.5),
      borderRadius: 6 * f,
    },
    label: {
      show: true,
      position: 'bottom' as const,
      distance: 4,
      color: n.unresolved ? GT.error : GT.text,
      fontWeight: n.isEntry || n.isEnding ? 600 : 400,
      formatter: (p: any) => truncate(p.name, 18),
    },
  }
}

/** 展开圆点 datum（同系列节点，卡片右侧外，+ 展开 / − 收起） */
function dotDatum(n: NodeItem, f: number): any {
  return {
    id: `__dot_${n.id}`,
    name: '',
    x: n.x + CARD_W / 2 + DOT_GAP,
    y: n.y,
    nodeId: n.id,
    symbol: 'circle',
    symbolSize: 22 * f,
    itemStyle: { color: GT.info, borderColor: GT.bg, borderWidth: 1.5 },
    label: {
      show: true,
      position: 'inside' as const,
      formatter: n.expanded ? '−' : '+',
      color: '#0c0c0f',
      fontWeight: 700,
    },
    // 悬浮圆点不需要 tooltip/高亮
    emphasis: { disabled: true },
  }
}

/** 悬浮详情（标题/摘要/进度/出场） */
function storyTooltip(n: NodeItem): string {
  const lines: string[] = [`<b>${n.title}</b>`]
  if (n.summary) {
    lines.push(`<div style="max-width:280px;white-space:normal;margin:4px 0">${n.summary}</div>`)
  }
  const meta: string[] = [`${n.dialogueCount} 句`]
  if (n.progress >= 0) meta.push(`翻译 ${n.progress}%`)
  if (n.isEntry) meta.push('入口')
  if (n.isEnding) meta.push('结局')
  lines.push(`<span style="opacity:.65">${meta.join(' · ')}</span>`)
  if (n.speakers.length) {
    lines.push(`<span style="opacity:.65">出场：${n.speakers.join('、')}</span>`)
  }
  if (n.expandCount) {
    lines.push('<span style="opacity:.45">点右侧 + 展开分支，双击卡片同效</span>')
  }
  return lines.join('')
}

function tooltipFormatter(p: any): string {
  if (p.dataType === 'edge') {
    const l = linkList[p.dataIndex]
    return l?.labelText || ''
  }
  const n = nodeList.find((x) => x.id === p.data?.id)
  return n ? storyTooltip(n) : ''
}

/** 组装当前可见数据 + 离线 dagre 布局 */
async function buildRenderData(): Promise<void> {
  const visible = new Map<string, ApiScene>()
  for (const s of data.value?.scenes ?? []) {
    if (visibleIds.value.has(s.scene_id)) visible.set(s.scene_id, s)
  }

  const nodes: NodeItem[] = []
  const dagreEdges: { id: string; source: string; target: string }[] = []
  const links: any[] = []
  for (const s of visible.values()) {
    const outEdges = (outMap.value.get(s.scene_id) ?? [])
      .filter((e) => !e.unresolved && sceneById.value.has(e.target))
    nodes.push({
      id: s.scene_id,
      x: 0, y: 0,
      h: s.thumb_file ? CARD_H_THUMB : CARD_H_PLAIN,
      title: s.title || s.scene_id,
      summary: s.summary,
      thumbUrl: s.thumb_file
        ? `/api/current/graph/story/thumb/${encodeURIComponent(s.thumb_file)}`
        : '',
      progress: progressOf(s),
      dialogueCount: s.dialogue_count,
      speakers: speakersOf(s),
      isEntry: !!s.is_entry,
      isEnding: !!s.is_ending,
      expandCount: outEdges.length,
      expanded: expanded.value.has(s.scene_id),
      unresolved: false,
    })
  }
  let needUnresolved = false
  for (const e of data.value?.edges ?? []) {
    if (!visible.has(e.source)) continue
    if (!expanded.value.has(e.source)) continue
    let target = e.target
    if (e.unresolved || target === UNRESOLVED_ID) {
      if (!showUnresolved.value) continue
      target = UNRESOLVED_ID
      needUnresolved = true
    } else if (!visible.has(target)) {
      continue
    }
    const id = `e${e.id}${target === UNRESOLVED_ID ? '_u' : ''}`
    dagreEdges.push({ id, source: e.source, target })
    links.push({
      id,
      source: e.source,
      target,
      labelText: truncate(edgeLabel(e), 40),
      lineStyle: {
        color: edgeColor(e),
        width: e.branch === 'menu' ? 1.8 : 1.2,
        type: e.has_call || e.unresolved ? 'dashed' as const : 'solid' as const,
      },
      label: { show: false, formatter: truncate(edgeLabel(e), 24) },
    })
  }
  if (needUnresolved) {
    nodes.push({
      id: UNRESOLVED_ID, x: 0, y: 0, h: 40,
      title: '未决跳转', summary: '', thumbUrl: '', progress: -1,
      dialogueCount: 0, speakers: [], isEntry: false, isEnding: false,
      expandCount: 0, expanded: false, unresolved: true,
    })
  }

  // 离线 dagre 布局（只要节点坐标；边由 echarts 原生绘制）
  const layout = new DagreLayout({
    rankdir: 'LR',
    nodesep: 40,
    ranksep: 140,
    ranker: 'network-simplex',
    nodeSize: (d: any) => d.data.size,
  } as any)
  await layout.execute({
    nodes: nodes.map((n) => ({
      id: n.id,
      data: { size: [CARD_W, n.h] as [number, number] },
    })),
    edges: dagreEdges,
  } as any)
  layout.forEachNode((n: any) => {
    const node = nodes.find((x) => x.id === String(n.id))
    if (node) {
      node.x = n.x
      node.y = n.y
    }
  })
  layout.destroy()

  // 边标签 = 隐形"标签节点"（symbol none，坐标 = 边中点）：
  // echarts 的 ec-line 标签定位基于包围盒，与可见线段对不上（默认
  // 'middle' 退化成左上角，数组百分比也不可靠）；做成 graph 数据点
  // 后 roam 变换原生跟随，位置完全可控
  edgeLabelNodes = []
  for (const l of links) {
    if (!l.labelText) continue
    const a = nodes.find((n) => n.id === l.source)
    const b = nodes.find((n) => n.id === l.target)
    if (!a || !b) continue
    const rad = Math.atan2(b.y - a.y, b.x - a.x)
    let deg = rad * 180 / Math.PI
    if (deg > 90) deg -= 180
    if (deg < -90) deg += 180
    // 法线方向取"线条上方"（y 向上为负；翻转角的法线方向会反，单独算）
    let nx = Math.sin(rad)
    let ny = -Math.cos(rad)
    if (ny > 0) { nx = -nx; ny = -ny }
    edgeLabelNodes.push({
      id: `__el_${l.id}`, name: '',
      x: (a.x + b.x) / 2, y: (a.y + b.y) / 2,
      // 隐形载体：symbol 'none' 连标签也不渲染；itemStyle.opacity=0
      // 会把标签一起隐藏——0 尺寸 + 透明填充才能只藏符号、留标签
      symbol: 'circle',
      symbolSize: 0.1,
      itemStyle: { color: 'rgba(0,0,0,0)' },
      silent: true,
      label: {
        show: true,
        formatter: truncate(l.labelText, 24),
        color: GT.text2,
        // 沿边方向倾斜（zrender rotation 正方向与 atan2 相反，取负），
        // 法线方向偏移让文字浮在线条上方而不是压线
        rotate: Math.round(-deg),
        offset: [Math.round(nx * 10), Math.round(ny * 10)],
        align: 'center' as const,
        verticalAlign: 'middle' as const,
      },
      emphasis: { disabled: true },
    })
  }

  nodeList = nodes
  linkList = links
}

/** 视口 = series.center（数据坐标）+ series.zoom（roam 原生） */
function setView(cx: number, cy: number, zoom?: number) {
  if (!chart) return
  const s: any = { center: [cx, cy] }
  if (zoom !== undefined) s.zoom = zoom
  chart.setOption({ series: [s] })
  scheduleLabelZoom()
}

/** 初始 fit：可见节点包围盒适配容器，得到有效缩放 s0（上限 1.2
 *  避免单卡撑满屏幕）；实际 zoom = s0 / fitScale（校准后设置） */
function fitTarget(): { cx: number; cy: number; s0: number } | null {
  if (!nodeList.length || !chart) return null
  const xs = nodeList.map((n) => n.x)
  const ys = nodeList.map((n) => n.y)
  const spanX = Math.max(...xs) - Math.min(...xs) + CARD_W + 120
  const spanY = Math.max(...ys) - Math.min(...ys) + CARD_H_THUMB + 160
  const rect = chart.getDom().getBoundingClientRect()
  const s0 = Math.min(
    1.2, rect.width / Math.max(spanX, 1), rect.height / Math.max(spanY, 1))
  return {
    cx: (Math.min(...xs) + Math.max(...xs)) / 2,
    cy: (Math.min(...ys) + Math.max(...ys)) / 2,
    s0: Math.max(s0, 0.05),
  }
}

/** 展开动画：视口平滑滑向目标（新节点入场动画由 graph series 原生提供） */
function playViewAnim(target: { cx: number; cy: number; zoom?: number }) {
  if (!chart || animating) return
  const opt = (chart.getOption().series as any[])?.[0] ?? {}
  const from = {
    cx: (opt.center?.[0] ?? target.cx) as number,
    cy: (opt.center?.[1] ?? target.cy) as number,
    zoom: currentZoom(),
  }
  const toZoom = target.zoom ?? from.zoom
  animating = true
  const t0 = performance.now()
  const dur = 380
  const ease = (t: number) => 1 - (1 - t) ** 3
  const tick = () => {
    if (!chart) { animating = false; return }
    const t = Math.min(1, (performance.now() - t0) / dur)
    const k = ease(t)
    chart.setOption({
      series: [{
        center: [
          from.cx + (target.cx - from.cx) * k,
          from.cy + (target.cy - from.cy) * k,
        ],
        zoom: from.zoom + (toZoom - from.zoom) * k,
      }],
    })
    if (t < 1) requestAnimationFrame(tick)
    else { animating = false; applyLabelZoom() }
  }
  requestAnimationFrame(tick)
}

function syncMinimapView() {
  if (!chart || !minimap) return
  const w = chart.getDom().clientWidth
  const h = chart.getDom().clientHeight
  const p0 = chart.convertFromPixel({ seriesIndex: 0 }, [0, 0])
  const p1 = chart.convertFromPixel({ seriesIndex: 0 }, [w, h])
  if (!p0 || !p1) return
  minimap.setView(
    Math.min(p0[0], p1[0]), Math.max(p0[0], p1[0]),
    Math.min(p0[1], p1[1]), Math.max(p0[1], p1[1]))
}

/** 悬浮放大的节点元素（纯 transform 倍率，与 zoom/fitScale 无关） */
const HOT_SCALE = 1.15
let hotEl: any = null

/** 设置/取消悬浮放大（只改元素 transform，不碰样式与状态机） */
function setHotNode(id: string | null) {
  const graph = (chart as any)?.getModel?.()
    ?.getSeriesByIndex(0)?.getGraph?.()
  if (!graph) return
  let el: any = null
  if (id !== null) {
    graph.eachNode((gn: any) => {
      if (!el && String(gn.id) === id) el = gn.getGraphicEl?.() ?? null
    })
  }
  if (hotEl === el) return
  if (hotEl) {
    hotEl.attr({
      scaleX: hotEl.scaleX / HOT_SCALE,
      scaleY: hotEl.scaleY / HOT_SCALE,
    })
    hotEl = null
  }
  if (el) {
    el.attr({ scaleX: el.scaleX * HOT_SCALE, scaleY: el.scaleY * HOT_SCALE })
    hotEl = el
  }
}

async function renderGraph() {
  try {
    await renderGraphInner()
  } catch (e) {
    console.error('[剧情图] 渲染失败:', e)
  }
}

async function renderGraphInner() {
  await nextTick()
  if (!container.value || !data.value || !hasGraph.value) return
  if (!chart) {
    chart = makeChart(container.value)
    chart.on('click', onChartClick)
    chart.on('dblclick', (p: any) => {
      if (p.dataType !== 'node') return
      const n = nodeList.find((x) => x.id === p.data?.id)
      if (n && n.expandCount > 0) toggleExpand(n.id)
    })
    chart.on('graphroam', () => {
      syncMinimapView()
      scheduleLabelZoom()
    })
    // 容器尺寸变化 → fitScale 变化 → 重新校准符号尺寸
    chart.on('resize', () => calibrateSizes())
    // 悬浮放大：直接对 zrender 元素乘固定倍率——与 echarts 的
    // emphasis.scale（与 fitScale 补偿冲突、会异常变大）不同，
    // 这个倍率在任何缩放级别下都是恒定的 1.15×
    chart.on('mouseover', (p: any) => {
      if (p.dataType !== 'node' || !p.data?.id || p.data.nodeId) return
      setHotNode(p.data.id)
    })
    chart.on('mouseout', () => setHotNode(null))
    chart.on('globalout', () => setHotNode(null))
    ;(window as any).__chart = chart
    ;(window as any).__toggleExpand = toggleExpand
  }
  if (!minimap && minimapEl.value) {
    minimap = new MiniMap(minimapEl.value, (x, y) => setView(x, y))
  }

  await buildRenderData()

  // 展开/收起前记住当前视图（有效缩放 = fitScale × zoom）：
  // 包围盒变化会让 fitScale 突变，位置瞬间重映射（"突然变大再重置"），
  // 渲染后用 zoom 反向补偿，保持内容在屏幕上稳定
  const prev = {
    zoom: currentZoom(),
    fit: fitScale,
    center: (chart.getOption?.()?.series as any[])?.[0]?.center as
      [number, number] | undefined,
  }
  const option: any = {
    backgroundColor: GT.bg,
    animation: true,
    animationDurationUpdate: 350,
    animationEasingUpdate: 'cubicOut',
    tooltip: {
      trigger: 'item',
      confine: true,
      backgroundColor: GT.card,
      borderColor: GT.border,
      borderWidth: 1,
      textStyle: { color: GT.text, fontSize: 12 },
      formatter: tooltipFormatter,
    },
    series: [{
      id: 'graph',
      type: 'graph',
      layout: 'none',
      roam: true,
      // 默认 roamTrigger 只在内容包围盒内响应滚轮/拖拽——节点少时
      // 画布大片区域缩放/拖动失灵；global = 全画布可 roam
      roamTrigger: 'global',
      // 缩放范围要覆盖 fitScale 的巨大摆幅：1 个节点时 fitScale≈7，
      // 祖先链全展开（800+ 节点的长链）时 fitScale≈0.004——补偿 zoom
      // 需要到几百倍才能保持节点视觉尺寸不变（有效缩放 = fitScale ×
      // zoom 恒定）；上限钳小了会导致大量展开后节点缩到没法看
      scaleLimit: { min: 0.01, max: 400 },
      // 符号随 zoom 等比缩放（默认 0.6 会让卡片尺寸与连线间距脱钩）
      nodeScaleRatio: 1,
      data: [
        ...nodeList.map((n) => nodeDatum(n, fitScale)),
        ...nodeList.filter((n) => n.expandCount > 0)
          .map((n) => dotDatum(n, fitScale)),
        ...edgeLabelNodes,
      ],
      links: linkList,
      edgeSymbol: ['none', 'arrow'],
      edgeSymbolSize: 7,
      // 禁用悬浮放大（默认 emphasis.scale=true 会与 fitScale 尺寸
      // 补偿冲突，大量展开时悬浮卡片会异常变大）
      emphasis: { scale: false },
      z: 2,
    }],
  }
  // 默认 merge：graph series 按 datum 差分，新增节点原生入场动画、
  // 旧节点保持；zoom/center 不给出，roam 状态原生保持
  chart.setOption(option)

  // 坐标系就绪后校准 fitScale（符号尺寸与 dagre 间距对齐）
  calibrateSizes()

  if (firstRender) {
    firstRender = false
    const t = fitTarget()
    if (t) {
      fitBase = t.s0
      chart.setOption({
        series: [{ center: [t.cx, t.cy], zoom: t.s0 / fitScale }],
      })
    }
  } else {
    // 补偿 fitScale 突变：zoom' = zoom × fitOld / fitNew，
    // 有效缩放与视图中心数据点都保持不变
    const z = Math.min(400, Math.max(0.01, prev.zoom * prev.fit / fitScale))
    chart.setOption({
      series: [{ zoom: z, ...(prev.center ? { center: prev.center } : {}) }],
    })
    if (focusAfterRender) {
      const id = focusAfterRender
      focusAfterRender = null
      const n = nodeList.find((x) => x.id === id)
      if (n) {
        // 聚焦被展开节点与其新出现子节点的中点，子节点不会被甩出屏幕
        const kids = nodeList.filter((k) => k.id !== id
          && (outMap.value.get(id) ?? []).some((e) => e.target === k.id))
        const pts = [n, ...kids]
        playViewAnim({
          cx: pts.reduce((s, p) => s + p.x, 0) / pts.length,
          cy: pts.reduce((s, p) => s + p.y, 0) / pts.length,
        })
      }
    }
  }
  applyLabelZoom()

  minimap?.setNodes(nodeList.map((n) => ({
    x: n.x, y: n.y,
    color: n.unresolved ? GT.error
      : n.isEntry ? GT.primary : n.isEnding ? GT.warning : GT.info,
    r: 6,
  })))
  syncMinimapView()
  // 调试/自动化测试句柄（webview 验证用）
  ;(window as any).__nodes = nodeList.map((n) => [n.id, n.x, n.y])
}

/** 点击：圆点（带 nodeId 的节点）→ 展开/收起；卡片节点 → 详情抽屉 */
function onChartClick(params: any) {
  if (params.dataType !== 'node') return
  const dotId = params.data?.nodeId
  if (dotId) {
    toggleExpand(dotId)
    return
  }
  const n = nodeList.find((x) => x.id === params.data?.id)
  if (!n || n.unresolved) return
  openScene(n.id)
}

/** 展开/收起一个场景 */
function toggleExpand(sceneId: string) {
  const next = new Set(expanded.value)
  if (next.has(sceneId)) next.delete(sceneId)
  else next.add(sceneId)
  expanded.value = next
  focusAfterRender = sceneId
}

// 搜索变化：重渲染后聚焦到第一个命中节点（路径已自动展开）
watch(search, () => {
  const hits = hitIds.value
  if (hits.size) {
    focusAfterRender = hits.values().next().value as string
  }
})

watch([search, fileFilter, showUnresolved, expanded, hasGraph], renderGraph,
  { flush: 'post' })
onMounted(load)
onBeforeUnmount(() => {
  cancelAnimationFrame(labelRaf)
  minimap?.dispose()
  chart?.dispose()
})

function gotoEnding(sceneId: string) {
  // 只展开必须的节点：沿最短路径树回溯到入口，仅把这条链标记展开
  const next = new Set(expanded.value)
  let cur = sceneId
  while (shortestParent.value.has(cur)) {
    cur = shortestParent.value.get(cur)!
    next.add(cur)
  }
  expanded.value = next
  focusAfterRender = sceneId
  if (chart) {
    openScene(sceneId)
  }
}

// ---- 场景详情抽屉 ----
const drawerOpen = ref(false)
const selected = ref<ApiScene | null>(null)
const dialogue = ref<DialogueLine[]>([])
const dialogueLoading = ref(false)

async function openScene(sceneId: string) {
  const s = data.value?.scenes.find((x) => x.scene_id === sceneId)
  if (!s) return
  selected.value = s
  drawerOpen.value = true
  dialogue.value = []
  dialogueLoading.value = true
  try {
    const r = await api.get<{ lines: DialogueLine[] }>(
      `/api/current/graph/scenes/${encodeURIComponent(sceneId)}/dialogue`)
    dialogue.value = r.lines
  } catch (e) {
    toastError(message, e)
  } finally {
    dialogueLoading.value = false
  }
}

const selectedProgress = computed(() => {
  const s = selected.value
  if (!s || !s.tl_total) return 100
  return Math.round(100 * s.translated_count / s.tl_total)
})

function speakersOfFull(s: ApiScene) {
  return parseJson(s.speakers_json).map((v) => ({
    variable: v,
    name: data.value?.characters[v]?.name ?? v,
    hasAvatar: data.value?.characters[v]?.avatar ?? false,
  }))
}
</script>

<template>
  <div class="page">
    <n-card size="small">
      <n-space align="center" justify="space-between" style="width: 100%">
        <n-space align="center">
          <n-text strong style="font-size: 15px">剧情分支图</n-text>
          <template v-if="hasGraph && data">
            <n-statistic label="场景" :value="data.scenes.length" />
            <n-statistic label="结局" :value="endings.length" />
            <n-text depth="3" style="font-size: 12px">
              构建于 {{ data.stats.built_at }} · 点卡片右侧 + 逐级展开分支
            </n-text>
          </template>
          <n-tag size="small" type="success">绿=全译</n-tag>
          <n-tag size="small" type="warning">黄=部分</n-tag>
          <n-tag size="small">灰=未译</n-tag>
        </n-space>
        <n-space align="center">
          <template v-if="hasGraph">
            <n-select :options="endingOptions" placeholder="跳转到结局…"
                      size="small" style="width: 200px" clearable filterable
                      @update:value="(v: string) => v && gotoEnding(v)" />
            <n-input v-model:value="searchInput"
                     placeholder="搜索标题/摘要/label"
                     clearable size="small" style="width: 180px" />
            <n-select v-model:value="fileFilter" :options="fileOptions"
                      placeholder="按文件过滤" clearable size="small"
                      style="width: 200px" />
            <n-switch v-model:value="showUnresolved" size="small">
              <template #checked>未决边</template>
              <template #unchecked>未决边</template>
            </n-switch>
          </template>
          <n-button size="small" :render-icon="renderIcon(RefreshOutline)"
                    @click="build">
            {{ hasGraph ? '重新构建' : '构建剧情图' }}
          </n-button>
        </n-space>
      </n-space>
    </n-card>

    <div class="body">
      <div v-if="loading" class="empty-wrap"><n-spin size="large" /></div>
      <div v-else-if="!hasGraph" class="empty-wrap">
        <n-empty description="尚未构建剧情图">
          <template #extra>
            <n-button type="primary" :render-icon="renderIcon(PlayOutline)"
                      @click="build">
              构建剧情图
            </n-button>
          </template>
        </n-empty>
      </div>
      <div v-show="hasGraph" ref="container" class="flow" />
      <div v-show="hasGraph" ref="minimapEl" class="minimap" />
    </div>

    <n-drawer v-model:show="drawerOpen" :width="440">
      <n-drawer-content v-if="selected" closable
                        :title="selected.title || selected.scene_id">
        <n-space vertical size="medium">
          <img v-if="selected.thumb_file"
               :src="`/api/current/graph/story/thumb/${encodeURIComponent(selected.thumb_file)}`"
               style="width: 100%; border-radius: 6px" alt="场景" />
          <n-space>
            <n-tag v-if="selected.is_entry" type="success" size="small">入口</n-tag>
            <n-tag v-if="selected.is_ending" type="warning" size="small">结局</n-tag>
            <n-tag v-if="selected.is_return" size="small">子流程返回点</n-tag>
            <n-tag size="small">{{ selected.scene_id }}</n-tag>
          </n-space>
          <p v-if="selected.summary" style="margin: 0; font-size: 13px; line-height: 1.6">
            {{ selected.summary }}
          </p>
          <div>
            <n-space justify="space-between" style="font-size: 12px">
              <n-text depth="2">翻译进度</n-text>
              <n-text depth="2">
                {{ selected.translated_count }}/{{ selected.tl_total }}
              </n-text>
            </n-space>
            <n-progress type="line" :percentage="selectedProgress"
                        :status="selectedProgress >= 100 ? 'success'
                          : selectedProgress > 0 ? 'warning' : 'default'"
                        :height="8" border-radius="4px" />
          </div>
          <n-text depth="2" style="font-size: 12px">
            {{ selected.file_path }}:{{ selected.line_start }} ·
            {{ parseJson(selected.labels_json).length }} 个 label
          </n-text>
          <div v-if="speakersOfFull(selected).length">
            <n-text strong style="font-size: 13px">出场人物</n-text>
            <div class="speaker-list">
              <span v-for="s in speakersOfFull(selected)" :key="s.variable"
                    class="speaker">
                <span class="speaker-avatar">
                  <span class="speaker-letter">{{ s.name.charAt(0) }}</span>
                  <img v-if="s.hasAvatar"
                       :src="`/api/current/graph/avatar/${encodeURIComponent(s.variable)}`"
                       loading="lazy" :alt="s.name" />
                </span>
                {{ s.name }}
              </span>
            </div>
          </div>
          <div>
            <n-text strong style="font-size: 13px">
              完整台词（{{ dialogue.length }}）
            </n-text>
            <n-spin :show="dialogueLoading">
              <div class="dialogue-list">
                <div v-for="(line, i) in dialogue" :key="i"
                     class="dialogue-item">
                  <span v-if="line.character" class="dlg-speaker">
                    {{ line.character_cn }}
                  </span>
                  <span v-else class="dlg-speaker narrator">旁白</span>
                  <div class="dlg-texts">
                    <div class="dlg-cn">
                      {{ line.translated_text || line.original_text }}
                    </div>
                    <div v-if="line.translated_text" class="dlg-orig">
                      {{ line.original_text }}
                    </div>
                  </div>
                </div>
                <n-empty v-if="!dialogueLoading && !dialogue.length"
                         description="该场景没有台词记录" size="small" />
              </div>
            </n-spin>
          </div>
          <div>
            <n-text strong style="font-size: 13px">内部 label</n-text>
            <p style="margin: 4px 0; font-size: 12px">
              {{ parseJson(selected.labels_json).join(' → ') }}
            </p>
          </div>
        </n-space>
      </n-drawer-content>
    </n-drawer>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 10px;
  /* 布局头 52px + 内容区上下 padding 各 16px；图容器必须拿到确定高度 */
  height: calc(100vh - 84px);
  box-sizing: border-box;
}
.body { flex: 1; min-height: 0; display: flex; position: relative; }
.empty-wrap {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}
.flow { flex: 1; min-height: 0; border-radius: 8px; overflow: hidden; }
.minimap {
  position: absolute;
  right: 10px;
  bottom: 10px;
  width: 200px;
  height: 130px;
  border-radius: 6px;
  border: 1px solid rgba(255,255,255,0.07);
  background: #1f1f24;
  overflow: hidden;
  z-index: 5;
}
.speaker-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 6px;
}
.speaker {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
}
.speaker-avatar {
  position: relative;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: #2a2a30;
  overflow: hidden;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.speaker-avatar img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: top;
}
.speaker-letter { font-size: 11px; color: rgba(255,255,255,0.45); }
.dialogue-list {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-right: 4px;
}
.dialogue-item {
  display: flex;
  gap: 8px;
  font-size: 12px;
  align-items: flex-start;
}
.dlg-speaker {
  flex-shrink: 0;
  min-width: 56px;
  font-weight: 600;
  color: #70c0e8;
}
.dlg-speaker.narrator { color: rgba(255,255,255,0.45); font-weight: 400; }
.dlg-cn { color: rgba(255,255,255,0.9); }
.dlg-orig { color: rgba(255,255,255,0.45); font-size: 11px; margin-top: 1px; }
</style>
