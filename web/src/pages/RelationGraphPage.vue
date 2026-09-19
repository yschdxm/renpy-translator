<script setup lang="ts">
/** 人物关系图谱（ECharts graph series layout:'none' + 手算圆环坐标：
 *  节点沿圆环排列，阵营分组排序，标签按角度朝外旋转；原生 roam 整体
 *  缩放。头像阵营色环 = 同系列矢量 circle 节点（任意缩放都清晰，
 *  不再烘进 PNG）；标签字号不随 roam 缩放是 echarts 固有行为，按
 *  zoom 手动补偿） */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  NButton, NCard, NDrawer, NDrawerContent, NEmpty, NForm, NFormItem,
  NInput, NModal, NPagination, NPopconfirm, NSelect, NSpace, NSpin,
  NSwitch, NTag,
  NText, useMessage,
} from 'naive-ui'
import { AddOutline, PlayOutline, RefreshOutline } from '@vicons/ionicons5'
import * as echarts from 'echarts'

import { api, toastError } from '../api/client'
import { renderIcon } from '../components/icons'
import {
  CATEGORY_META, FACTION_PALETTE, GT,
} from '../components/graph-theme'
import { MiniMap, makeChart } from '../components/echarts-utils'
import { useJobsStore } from '../stores/jobs'

interface Character {
  key: string
  variable: string
  display_name: string
  cn_name: string
  lines: number
  faction: string
  has_avatar: boolean
  avatar_path: string
  avatar_candidates: string[]
}
interface Relation {
  id: number
  source_var: string
  target_var: string
  relation: string
  category: string
  polarity: string
  description: string
  cooccurrence: number
  source: string
}
interface RelationsData { characters: Character[]; relations: Relation[] }

const CATEGORY_OPTIONS = Object.entries(CATEGORY_META)
  .filter(([k]) => k !== 'cooccur')
  .map(([value, m]) => ({ label: m.label, value }))

const message = useMessage()
const jobsStore = useJobsStore()

const loading = ref(true)
const data = ref<RelationsData | null>(null)
/** 共现开关：是否显示共现边（同场出场统计出来的弱关系，占边数一半
 *  以上，全显会糊成毛线团；默认关闭只显示 AI/人工关系） */
const showCooccur = ref(false)
const factionFilter = ref<string | null>(null)

async function load() {
  loading.value = true
  try {
    data.value = await api.get<RelationsData>('/api/current/graph/relations')
  } catch (e) {
    toastError(message, e)
  } finally {
    loading.value = false
  }
}

/** 引擎渲染（实验）：与剧情图页共用一个偏好键——开一处两处生效 */
const engineRender = ref(false)
try {
  engineRender.value = localStorage.getItem('sg_engine_render') === '1'
} catch { /* 只读环境 */ }
watch(engineRender, (v) => {
  try {
    localStorage.setItem('sg_engine_render', v ? '1' : '0')
  } catch { /* 同上 */ }
})

async function build() {
  try {
    const d = await api.post<{ job_id: string }>(
      '/api/current/graph/relations/build',
      { engine_render: engineRender.value })
    jobsStore.track(d.job_id)
  } catch (e) {
    toastError(message, e)
  }
}

watch(
  () => [...jobsStore.jobs.values()]
    .filter((j) => j.kind === 'graph.relations-build')
    .map((j) => `${j.id}:${j.status}`).join(','),
  () => { load() },
)

const charMap = computed(() => {
  const m = new Map<string, Character>()
  for (const c of data.value?.characters ?? []) m.set(c.key, c)
  return m
})

// 主角 = 台词数第一
const protagonistKey = computed(() => {
  let top: Character | null = null
  for (const c of data.value?.characters ?? []) {
    if (!top || c.lines > top.lines) top = c
  }
  return top?.key ?? ''
})

const factions = computed(() => {
  const counts = new Map<string, number>()
  for (const c of data.value?.characters ?? []) {
    if (c.faction) counts.set(c.faction, (counts.get(c.faction) ?? 0) + 1)
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1])
})

function factionColor(f: string): string {
  const idx = factions.value.findIndex(([name]) => name === f)
  return idx >= 0 ? FACTION_PALETTE[idx % FACTION_PALETTE.length] : '#909399'
}

const visibleRelations = computed(() =>
  (data.value?.relations ?? []).filter(
    (r) => r.source !== 'cooccur' || showCooccur.value))

const hasGraph = computed(() => visibleRelations.value.length > 0)

function nodeSize(lines: number): number {
  return Math.round(Math.min(84, Math.max(44, 44 + Math.sqrt(lines) * 2.2)))
}

// ---- ECharts 实例管理 ----
const container = ref<HTMLElement | null>(null)
const minimapEl = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null
let minimap: MiniMap | null = null
/** 阵营聚焦的 highlight 集合（切换时先 downplay） */
let highlightedIdx: number[] = []
/** 圆环布局各节点的坐标（手算，供小地图/定位） */
const layoutPos = new Map<string, [number, number]>()
/** 角色节点在 series.data 中的下标（阵营聚焦 dispatchAction 用） */
const charIndexOf = new Map<string, number>()
/** 当前渲染的关系列表（点击边/tooltip 的 dataIndex 共用） */
let currentRels: Relation[] = []
/** echarts graph 视图把内容包围盒 fit 进容器，roam zoom 乘在上面：
 *  位置缩放 = fitScale × zoom，符号缩放 = zoom。符号尺寸必须乘以
 *  fitScale 才能与坐标间距一致——只在数据/容器变化时校准一次 */
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

/** 上次渲染的节点构造函数（resize 重校准符号尺寸用） */
let rebuildNodes: ((f: number) => any[]) | null = null
/** 各角色节点在圆环上的角度（labelLayout 回调计算标签位置用） */
const angleOf = new Map<string, number>()
/** 各角色节点的尺寸（单位，labelLayout 回调算屏幕半径用） */
const sizeOf = new Map<string, number>()
/** series.data 下标 → 节点 id（labelLayout 回调反查用） */
let dataIds: string[] = []

/** 当前 roam 缩放（直读 series model：roam action 在标签布局之前已把
 *  zoom 同步进 model，比 convertToPixel 快且同帧无延迟） */
function modelZoom(): number {
  const z = (chart as any)?.getModel?.()
    ?.getSeriesByIndex(0)?.getShallow?.('zoom')
  return typeof z === 'number' && z > 0 ? z : 1
}

/** 标签布局回调（官方管线：每次渲染/roam 重布局时逐标签调用。
 *  位置/旋转/字号在同一遍布局里一起给出。注意不能用 params.rect
 *  （hostRect 是渲染时缓存，roam 重布局时是旧值），节点屏幕位置必须
 *  用 convertToPixel 现场算。字号与锚点半径只跟渲染后的节点半径
 *  挂钩（nodeR = 尺寸单位 × fitScale × zoom / 2，与几何天然一致），
 *  不依赖 fitBase 之类的状态量——杜绝字体莫名变大/缩小的路径 */
function nodeLabelLayout(params: any): any {
  if (params.dataType !== 'node') return { hideOverlap: true }
  const id = dataIds[params.dataIndex]
  const ang = id ? angleOf.get(id) : undefined
  const pos = id ? layoutPos.get(id) : undefined
  if (ang === undefined || !pos || !chart) return { hideOverlap: true }
  let px: number[]
  try {
    px = chart.convertToPixel({ seriesIndex: 0 }, [pos[0], pos[1]]) as number[]
  } catch {
    return { hideOverlap: true }   // 坐标系未就绪（首帧），下遍布局补齐
  }
  if (!px) return { hideOverlap: true }
  // 渲染后的节点半径（px）——字号/锚点间隙都按它的固定比例给出，
  // 任何缩放级别下标签与节点严格同比
  const nodeR = (sizeOf.get(id) ?? 40) * fitScale * modelZoom() / 2
  const r = nodeR * 3
  const isLeft = Math.cos(ang) < 0
  let deg = (ang * 180) / Math.PI
  if (isLeft) deg -= 180
  return {
    x: px[0] + r * Math.cos(ang),
    y: px[1] + r * Math.sin(ang),
    rotate: Math.round(-deg),
    fontSize: Math.min(64, Math.max(7, (11 * nodeR) / 5)),
    hideOverlap: true,
  }
}

/** 色环节点在 zrender 层静默（per-datum silent 对 graph 不生效，
 *  环外沿会被悬停：tooltip 露出内部 id、邻接高亮只算到环自己）。
 *  el.silent 后悬停穿透到下层头像，blur 淡出样式不受影响。
 *  每次 setOption 后都要调用（元素会随渲染重建） */
function silenceRingEls() {
  if (!chart) return
  const graph = (chart as any).getModel?.()
    ?.getSeriesByIndex(0)?.getGraph?.()
  graph?.eachNode((gn: any) => {
    if (!String(gn.id).startsWith('__ring_')) return
    const el = gn.getGraphicEl?.()
    if (el) el.silent = true
  })
}

/** 悬浮提示：头像 → 该角色全部关系；边 → 单条关系名称 */
function tooltipFormatter(p: any): string {
  if (p.dataType === 'edge') {
    const r = currentRels[p.dataIndex]
    if (!r) return ''
    return `${charName(r.source_var)} ↔ ${charName(r.target_var)}：${r.relation || '（未命名）'}`
  }
  const key = p.data?.id as string
  const list = currentRels.filter(
    (r) => r.source_var === key || r.target_var === key)
  if (!list.length) return charName(key)
  const rows = list.map((r) => {
    const other = r.source_var === key ? r.target_var : r.source_var
    const color = (CATEGORY_META[r.category] ?? CATEGORY_META.other).color
    return `<span style="color:${color}">●</span> ${r.relation || '（未命名）'} · ${charName(other)}`
  })
  return `<b>${charName(key)}</b><br/>${rows.join('<br/>')}`
}

async function renderGraph() {
  await nextTick()
  if (!container.value || !data.value || !hasGraph.value) return
  if (!chart) {
    chart = makeChart(container.value)
    chart.on('click', onChartClick)
    chart.on('graphroam', (e: any) => {
      syncMinimapView()
      relayoutLabelsOnPan(e)
      // 标签 model 字号跟随当前几何（直接改 model，不触发渲染）：
      // 状态切换渲染（悬浮/离开的 emphasis/blur）按 label model 重设
      // 标签样式——model 字号若停留在初始值，mouseout 后字体就会
      // "莫名缩放"回旧尺寸
      syncModelFontSize()
    })
    // 色环的 zrender 静默：echarts 每次渲染都会按 model 状态覆盖
    // el.silent，必须在每次渲染完成后重设（赋值不触发渲染，无循环）。
    // 标签 tspan 置为 stateful=false：不参与 emphasis/blur 状态机——
    // 状态机的样式快照/恢复机制是"悬浮后字体莫名缩放"的根源
    // （进入时快照、退出时恢复旧值，回调的字号更新全被覆盖）。
    // 置为无状态后：快照不存在，恢复不存在，字号只由布局回调决定。
    // 代价：悬浮时标签不再随节点变暗（可接受）
    chart.on('rendered', () => {
      silenceRingEls()
      statelessLabels()
      refreshLabelFonts()
      // 插桩：渲染后仍偏差 >30% 的标签（快照恢复漏网的路径）
      const bad: string[] = []
      const zoom = modelZoom()
      ;(chart as any).getZr().storage.getDisplayList().forEach((el: any) => {
        if (el.type !== 'tspan' || !el.style.text || bad.length >= 5) return
        const want = expectedFontOf(el.style.text)
        if (want && Math.abs((el.style.fontSize || 0) - want) / want > 0.3) {
          bad.push(`${el.style.text}:${Math.round(el.style.fontSize)}≠${Math.round(want)}(stateful=${el.stateful},op=${el.style.opacity ?? 'u'})`)
        }
      })
      if (bad.length) rgLog('rendered 后仍异常:', bad.join(' '), 'zoom=', zoom.toFixed(2))
    })
    // 悬浮离开兜底：先覆写标签样式（字号/缩放/透明度），再用系列
    // setOption 触发完整标签布局管线（hideOverlap 按正确字号重判
    // 该藏的）。不用 roam dispatch——它的 payloadDisableAnimation 会
    // 把恢复动画冻结在中途（透明度卡在 0.4 之类的中间值）
    const deferRefreshFonts = () => {
      setTimeout(() => {
        const n = refreshLabelFonts(true)
        const fs = syncModelFontSize()
        chart?.setOption({ series: [{ label: { fontSize: fs } }] })
        rgLog('mouseout 兜底刷新, 修正', n, '个标签')
      }, 60)
    }
    chart.on('mouseover', (p: any) => {
      rgLog('hover in', p.dataType, p.data?.id ?? p.dataIndex)
    })
    chart.on('mouseout', (p: any) => {
      rgLog('hover out', p.dataType, p.data?.id ?? p.dataIndex)
      if (p.dataType === 'node' || p.dataType === 'edge') {
        deferRefreshFonts()
        // 鼠标离开图元素会连程序化高亮一起清掉——详情开着时补回
        if (cardOpen.value && cardChar.value) {
          setCardHighlight(cardChar.value.key)
        }
      }
    })
    chart.on('globalout', () => {
      rgLog('globalout')
      deferRefreshFonts()
      if (cardOpen.value && cardChar.value) {
        setCardHighlight(cardChar.value.key)
      }
    })
    // 容器尺寸变化 → fitScale 变化 → 重新校准符号尺寸
    chart.on('resize', () => {
      const f = effectiveScale() / currentZoom()
      if (Math.abs(f - fitScale) / Math.max(fitScale, 1e-6) > 0.03) {
        fitScale = f
        if (rebuildNodes) {
          chart?.setOption({ series: [{ data: rebuildNodes(f) }] })
          silenceRingEls()
        }
      }
    })
    ;(window as any).__chart = chart
  }
  if (!minimap && minimapEl.value) {
    minimap = new MiniMap(minimapEl.value, (x, y) => {
      // 小地图点击：定位到最近的节点
      let best: string | null = null
      let bestD = Infinity
      for (const [id, p] of layoutPos) {
        const d = (p[0] - x) ** 2 + (p[1] - y) ** 2
        if (d < bestD) { bestD = d; best = id }
      }
      const p = best ? layoutPos.get(best) : null
      if (p) animateCenterTo(p[0], p[1])
    })
  }

  const rels = visibleRelations.value.filter(
    (r) => charMap.value.has(r.source_var) && charMap.value.has(r.target_var))
  currentRels = rels

  // 环形排序：阵营分组（大阵营在前），组内按台词数降序，主角在最前——
  // 同阵营相邻减少跨环长边
  const keys = new Set<string>()
  for (const r of rels) {
    keys.add(r.source_var)
    keys.add(r.target_var)
  }
  const ordered = [...keys].sort((a, b) => {
    if (a === protagonistKey.value) return -1
    if (b === protagonistKey.value) return 1
    const ca = charMap.value.get(a)!
    const cb = charMap.value.get(b)!
    const fa = factions.value.findIndex(([f]) => f === ca.faction)
    const fb = factions.value.findIndex(([f]) => f === cb.faction)
    if (fa !== fb) return (fa < 0 ? 99 : fa) - (fb < 0 ? 99 : fb)
    return cb.lines - ca.lines
  })

  // 手算圆环坐标：阵营分组（大阵营在前），组内按台词数降序，主角在
  // 最前（顶部）——同阵营相邻减少跨环长边；环半径保证相邻节点间距
  const N = ordered.length
  const R = Math.max(260, Math.round((N * 78) / (2 * Math.PI)))
  /** symbolSize × fitScale 后与坐标间距严格一致（roam 时整体比例缩放） */
  const buildNodes = (f: number): any[] => {
    const nodes: any[] = []
    layoutPos.clear()
    charIndexOf.clear()
    angleOf.clear()
    sizeOf.clear()
    dataIds = []
    ordered.forEach((key, i) => {
      const c = charMap.value.get(key)!
      const color = c.faction ? factionColor(c.faction) : '#909399'
      const angle = -Math.PI / 2 + (i * 2 * Math.PI) / N   // 顶部起顺时针
      const x = R * Math.cos(angle)
      const y = R * Math.sin(angle)
      layoutPos.set(key, [x, y])
      angleOf.set(key, angle)
      const size = Math.round(nodeSize(c.lines) * 0.7)
      sizeOf.set(key, size)
      const isLeft = x < 0
      if (c.has_avatar) {
        // 矢量色环：同坐标透明圆节点（silent 不挡事件），阵营色描边，
        // 任意缩放都清晰——不再把色环烘进 PNG。
        // 不要设 emphasis.disabled：它会连 blur 淡出一起屏蔽，
        // 导致无关联者的环常亮（隐形邻接边负责让被悬浮者的环保持亮）
        nodes.push({
          id: `__ring_${key}`, name: '', x, y,
          symbol: 'circle', symbolSize: (size + 8) * f,
          itemStyle: {
            color: 'rgba(0,0,0,0)', borderColor: color,
            borderWidth: 3 * Math.max(f, 0.5),
          },
          label: { show: false },
          silent: true,
        })
      }
      dataIds.push(...Array(nodes.length - dataIds.length)
        .fill('') as string[])
      dataIds[nodes.length - 1] = c.has_avatar ? `__ring_${key}` : ''
      charIndexOf.set(key, nodes.length)
      nodes.push({
        id: key,
        name: `${key === protagonistKey.value ? '★ ' : ''}${c.cn_name || c.display_name}`,
        x, y,
        symbol: c.has_avatar
          ? `image:///api/current/graph/avatar_circle/${encodeURIComponent(key)}?v=${avatarVer.value}`
          : 'circle',
        symbolSize: size * f,
        itemStyle: c.has_avatar
          ? undefined
          : { color: '#2a2a30', borderColor: color,
              borderWidth: 3 * Math.max(f, 0.5) },
        label: {
          show: true,
          color: GT.text,
          // 文字沿半径朝外（锚点/旋转由 labelLayout 回调按当前视图计算）
          align: (isLeft ? 'right' : 'left') as any,
          verticalAlign: 'middle' as any,
        },
      })
    })
    dataIds = nodes.map((n) => n.id)
    return nodes
  }
  rebuildNodes = buildNodes
  const links: any[] = rels.map((r) => ({
    source: r.source_var,
    target: r.target_var,
    lineStyle: {
      color: (CATEGORY_META[r.category] ?? CATEGORY_META.other).color,
      width: Math.min(4, 0.8 + (r.cooccurrence || 0) / 6),
      type: r.source === 'cooccur' ? 'dotted' as const
        : r.polarity === 'negative' ? 'dashed' as const : 'solid' as const,
      opacity: 0.85,
    },
    label: { show: false, formatter: r.relation },
  }))
  // 色环 → 所属角色的隐形邻接边（零长度不可见）：把悬浮淡出（blur）
  // 的邻接判断传递到色环——无关联者的环随头像一起暗，被悬浮者及
  // 其关联者的环保持亮。追加在真实边之后，currentRels[dataIndex]
  // 取到 undefined，点击/tooltip 自然忽略
  for (const key of ordered) {
    if (charMap.value.get(key)?.has_avatar) {
      links.push({
        source: `__ring_${key}`, target: key,
        lineStyle: { opacity: 0, width: 0 },
        label: { show: false },
      })
    }
  }

  chart.setOption({
    backgroundColor: GT.bg,
    tooltip: {
      // 悬浮头像 → 该角色的全部关系；悬浮边 → 单条关系名称
      trigger: 'item',
      confine: true,
      backgroundColor: GT.card,
      borderColor: GT.border,
      borderWidth: 1,
      textStyle: { color: GT.text, fontSize: 12 },
      formatter: tooltipFormatter,
    },
    series: [{
      type: 'graph',
      layout: 'none',
      roam: true,
      // 默认 roamTrigger 只在内容包围盒内响应滚轮/拖拽；global = 全画布
      roamTrigger: 'global',
      scaleLimit: { min: 0.1, max: 6 },
      // 符号随 zoom 等比缩放（默认 0.6 会让头像尺寸与坐标间距脱钩）
      nodeScaleRatio: 1,
      draggable: false,
      data: buildNodes(fitScale),
      links,
      center: [0, 0],
      emphasis: {
        focus: 'adjacency',   // 悬停一阶关系高亮（其余淡出）
        scale: false,         // 禁用悬浮放大（与 fitScale 尺寸补偿冲突）
        label: { fontWeight: 'bold' },
      },
      labelLayout: nodeLabelLayout,   // 放射状标签 + hideOverlap（回调内）
      lineStyle: { curveness: 0.12 },
      z: 2,
    }],
  }, { replaceMerge: ['series'] })

  // 坐标系就绪后校准 fitScale（符号尺寸与坐标间距对齐），再按有效缩放
  // fit 整环进容器短边（含节点与外侧标签余量）
  {
    const f = effectiveScale() / currentZoom()
    if (Math.abs(f - fitScale) / Math.max(fitScale, 1e-6) > 0.03) {
      fitScale = f
      chart.setOption({ series: [{ data: buildNodes(fitScale) }] })
    }
    const rect = container.value.getBoundingClientRect()
    const s0 = Math.min(
      1, Math.min(rect.width, rect.height) / (2 * (R + 60 + 110)))
    fitBase = Math.max(s0, 0.1)
    chart.setOption({
      series: [{ center: [0, 0], zoom: fitBase / fitScale }],
    })
  }
  silenceRingEls()
  syncModelFontSize()

  // 阵营聚焦恢复（重建 series 会清掉 highlight 状态）
  if (factionFilter.value) {
    applyFactionDim(factionFilter.value)
  }
  // 详情高亮恢复（详情开着时重渲染会清掉 highlight 状态）
  if (cardOpen.value && cardChar.value) {
    setCardHighlight(cardChar.value.key)
  }

  minimap?.setNodes([...layoutPos.entries()].map(([id, p]) => {
    const c = charMap.value.get(id)!
    return {
      x: p[0], y: p[1],
      color: c.faction ? factionColor(c.faction) : '#909399',
      r: 5,
    }
  }))
  syncMinimapView()
  rgLog('render', RG_BUILD, 'nodes=', layoutPos.size)
  // 调试/自动化测试句柄（webview 验证用）
  ;(window as any).__nodes = [...layoutPos.entries()]
    .map(([id, p]) => [id, p[0], p[1]])
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

/** 平移不会触发标签重布局（echarts 只在 zoom payload 时
 *  updateLabelLayout），全局坐标的标签会滞留原地——pan 时补发一个
 *  zoom=1 的空 roam action 强制重布局。
 *  用 payload.zoom == null 区分 pan/zoom：重发的 action 带 zoom，
 *  回到本函数时不再重复 dispatch（不会递归） */
/** 强制标签重布局：发一个 zoom=1 的空 roam action，走
 *  __updateOnOwnRoam → updateLabelLayout（视图本身不变）。
 *  用于 pan（echarts 只在 zoom payload 时重布局）和 mouseout
 *  （悬浮后缩放，离开时标签样式从过期缓存恢复）两种场景 */
function forceLabelRelayout() {
  if (!chart) return
  chart.dispatchAction({
    type: 'graphRoam',
    zoom: 1,
    originX: chart.getWidth() / 2,
    originY: chart.getHeight() / 2,
  } as any)
}

/** pan 时才补发（用 payload.zoom == null 区分 pan/zoom：
 *  重发的 action 带 zoom，回到本函数时不再重复 dispatch——不会递归） */
function relayoutLabelsOnPan(e: any) {
  if (!chart || e?.zoom != null) return
  forceLabelRelayout()
}

// ---- 临时插桩（排查字体缩放/淡出异常，定位后移除） ----
const RG_BUILD = '20260828-7'
function rgLog(...args: any[]) {
  console.log('[RG]', ...args)
}
/** 状态快照：bug 出现时用户在控制台执行 __rgDump() 并粘贴结果 */
;(window as any).__rgDump = () => {
  if (!chart) return 'no chart'
  const fonts: any[] = []
  ;(chart as any).getZr().storage.getDisplayList().forEach((el: any) => {
    if (el.type === 'tspan' && el.style.text && fonts.length < 30) {
      fonts.push({
        t: String(el.style.text).slice(0, 8),
        fs: Math.round((el.style.fontSize || 0) * 10) / 10,
        sx: Math.round((el.scaleX ?? 1) * 100) / 100,
        sy: Math.round((el.scaleY ?? 1) * 100) / 100,
        op: el.style.opacity ?? 'u',
        stf: el.stateful,
        ign: !!el.ignore,
      })
    }
  })
  return {
    build: RG_BUILD,
    zoom: modelZoom(),
    fitScale: Math.round(fitScale * 1000) / 1000,
    fonts,
  }
}

/** 把可见标签 tspan 置为无状态（stateful=false）：
 *  emphasis/blur 状态切换不再触碰标签（不再保存/恢复快照），
 *  从根源消除"悬浮期间缩放、鼠标离开后字体被恢复成旧值" */
function statelessLabels() {
  if (!chart) return
  ;(chart as any).getZr().storage.getDisplayList().forEach((el: any) => {
    if (el.type === 'tspan' && el.style.text && el.stateful !== false) {
      el.stateful = false
    }
  })
}

/** 某角色名在当前几何下的应有字号（插桩/刷新共用） */
function expectedFontOf(name: string): number | null {
  for (const [id] of layoutPos) {
    const c = charMap.value.get(id)
    if (!c) continue
    const n = `${id === protagonistKey.value ? '★ ' : ''}${c.cn_name || c.display_name}`
    if (n === name) {
      const nodeR = (sizeOf.get(id) ?? 40) * fitScale * modelZoom() / 2
      return Math.min(64, Math.max(7, (11 * nodeR) / 5))
    }
  }
  return null
}

/** 把可见标签 tspan 的字号/缩放/透明度刷成当前渲染几何的应有值。
 *  blur/emphasis 进入时 zrender 保存样式快照，退出恢复时旧值会
 *  覆盖回调的更新（字号旧、scale 旧）；hideOverlap 按恢复出的巨大
 *  字号判定全部重叠，还会把标签透明度动画到 0（"文字消失"）。
 *  按标签文本反查角色覆写。只碰样式，不碰位置/旋转/状态机 */
function refreshLabelFonts(restoreOpacity = false): number {
  if (!chart || !layoutPos.size) return 0
  const zoom = modelZoom()
  const fsOf = new Map<string, number>()
  for (const [id] of layoutPos) {
    const c = charMap.value.get(id)
    if (!c) continue
    const nodeR = (sizeOf.get(id) ?? 40) * fitScale * zoom / 2
    const name = `${id === protagonistKey.value ? '★ ' : ''}${c.cn_name || c.display_name}`
    fsOf.set(name, Math.min(64, Math.max(7, (11 * nodeR) / 5)))
  }
  let fixed = 0
  ;(chart as any).getZr().storage.getDisplayList().forEach((el: any) => {
    if (el.type !== 'tspan' || !el.style.text) return
    const fs = fsOf.get(el.style.text)
    if (fs && Math.abs((el.style.fontSize || 0) - fs) > 0.5) {
      el.setStyle('fontSize', fs)
      fixed++
    }
    // 快照恢复也会回滚 scale（style.fontSize 正确但视觉缩放错误）
    if (Math.abs((el.scaleX ?? 1) - 1) > 0.01
        || Math.abs((el.scaleY ?? 1) - 1) > 0.01) {
      el.attr({ scaleX: 1, scaleY: 1 })
      fixed++
    }
    // hideOverlap 按过期字号把标签淡出到 0——复位透明度
    //（随后 forceLabelRelayout 会按正确字号重新判定该藏的）
    if (restoreOpacity && el.style.opacity !== undefined
        && el.style.opacity < 0.99) {
      el.setStyle('opacity', 1)
      fixed++
    }
  })
  return fixed
}

/** 把当前几何应有的平均标签字号写进 series label model（直接改
 *  model option，不触发渲染管线的廉价操作）。
 *  作用：emphasis/blur 状态切换触发的渲染按 label model 重设标签
 *  样式——model 字号保持新鲜后，悬浮进出缩放过的视图，mouseout
 *  恢复的字体尺寸就是当前值，不会跌回初始值。返回写入的字号 */
function syncModelFontSize(): number {
  if (!chart || !sizeOf.size) return 11
  let sum = 0
  for (const s of sizeOf.values()) sum += s
  const avgR = (sum / sizeOf.size) * fitScale * modelZoom() / 2
  const fs = Math.min(64, Math.max(7, (11 * avgR) / 5))
  const labelModel = (chart as any).getModel?.()
    ?.getSeriesByIndex(0)?.getModel?.('label')
  if (labelModel) labelModel.option.fontSize = Math.round(fs * 10) / 10
  return Math.round(fs * 10) / 10
}

function onChartClick(params: any) {
  if (params.dataType === 'node') {
    openCharCard(params.data.id)
  } else if (params.dataType === 'edge') {
    const r = currentRels[params.dataIndex]
    if (!r) return
    editing.value = r
    editForm.value = {
      relation: r.relation,
      category: r.category in CATEGORY_META && r.category !== 'cooccur'
        ? r.category : 'other',
      polarity: r.polarity,
      description: r.description,
    }
    drawerOpen.value = true
  }
}

watch([data, showCooccur, hasGraph], renderGraph, { flush: 'post' })
onMounted(load)
onBeforeUnmount(() => {
  minimap?.dispose()
  chart?.dispose()
})

// 阵营 chips：聚焦同阵营（highlight 同阵营节点，其余随 emphasis 淡出）
function applyFactionDim(f: string | null) {
  if (!chart) return
  if (highlightedIdx.length) {
    chart.dispatchAction({
      type: 'downplay', seriesIndex: 0, dataIndex: highlightedIdx,
    })
    highlightedIdx = []
  }
  if (!f || !chart) return
  const idxs: number[] = []
  for (const [key, idx] of charIndexOf) {
    if (charMap.value.get(key)?.faction === f) idxs.push(idx)
  }
  if (idxs.length) {
    chart.dispatchAction({
      type: 'highlight', seriesIndex: 0, dataIndex: idxs,
    })
    highlightedIdx = idxs
  }
}

function highlightFaction(f: string) {
  factionFilter.value = factionFilter.value === f ? null : f
  applyFactionDim(factionFilter.value)
}

// ---- 角色卡抽屉 ----
const cardOpen = ref(false)
const cardChar = ref<Character | null>(null)
const cardProfile = ref<Record<string, string> | null>(null)
/** 抽屉打开期间保持高亮的节点下标（关抽屉/切换时先 downplay） */
let cardHighlightIdx: number | null = null

/** 详情打开期间保持该角色及关联的高亮（邻接 emphasis 同悬浮） */
function setCardHighlight(key: string | null) {
  if (!chart) return
  if (cardHighlightIdx !== null) {
    chart.dispatchAction({
      type: 'downplay', seriesIndex: 0, dataIndex: cardHighlightIdx,
    })
    cardHighlightIdx = null
  }
  if (key === null) return
  const idx = charIndexOf.get(key)
  if (idx !== undefined) {
    chart.dispatchAction({ type: 'highlight', seriesIndex: 0, dataIndex: idx })
    cardHighlightIdx = idx
  }
}

watch(cardOpen, (v) => {
  if (!v) setCardHighlight(null)
})

const cardRelations = computed(() => {
  if (!cardChar.value || !data.value) return []
  const k = cardChar.value.key
  return data.value.relations.filter(
    (r) => r.source_var === k || r.target_var === k)
})

async function openCharCard(key: string) {
  const c = charMap.value.get(key)
  if (!c) return
  cardChar.value = c
  cardProfile.value = null
  cardOpen.value = true
  setCardHighlight(key)
  try {
    const r = await api.get<{ profile: Record<string, string> }>(
      `/api/current/names/${encodeURIComponent(c.display_name)}/profile`)
    cardProfile.value = r.profile
  } catch {
    cardProfile.value = null  // 未分析过档案：仅显示基础信息
  }
}

// ---- 手动更换头像（模态框随用随调，关闭即销毁） ----
const avatarPickerOpen = ref(false)
const pickAvatar = ref('')
const avatarPage = ref(1)
/** 头像版本号：更换后自增，图上/详情头像 URL 带 v 参数破浏览器缓存 */
const avatarVer = ref(0)

function avatarCands(c: Character): string[] {
  return c.avatar_candidates ?? []
}

const avatarTotal = computed(() =>
  cardChar.value ? avatarCands(cardChar.value).length : 0)

const pagedAvatars = computed(() => {
  if (!cardChar.value) return []
  const all = avatarCands(cardChar.value)
  return all.slice((avatarPage.value - 1) * 12, avatarPage.value * 12)
})

function openAvatarPicker() {
  const c = cardChar.value
  if (!c) return
  pickAvatar.value = c.avatar_path || ''
  avatarPage.value = 1
  avatarPickerOpen.value = true
}

async function applyAvatarPref() {
  const c = cardChar.value
  if (!c || !pickAvatar.value) return
  try {
    await api.post('/api/current/graph/relations/avatar_pref',
                   { variable: c.variable, path: pickAvatar.value })
    avatarPickerOpen.value = false
    // 局部刷新：URL 版本号破缓存，只重建节点符号（布局/缩放不动）
    c.avatar_path = pickAvatar.value
    avatarVer.value = Date.now()
    if (chart && rebuildNodes) {
      chart.setOption({ series: [{ data: rebuildNodes(fitScale) }] })
    }
  } catch (e) {
    toastError(message, e)
  }
}

function charName(key: string): string {
  const c = charMap.value.get(key)
  return c ? (c.cn_name || c.display_name) : key
}

/** 中心跳转动画：拆成逐帧 pan roam action（与手动拖动同一管线，
 *  标签经 relayoutLabelsOnPan 逐帧重布局跟随；
 *  setOption center 的内部动画不重跑标签布局，文字会慢半拍） */
let centerAnimRaf = 0
function animateCenterTo(tx: number, ty: number) {
  if (!chart) return
  cancelAnimationFrame(centerAnimRaf)
  const w = chart.getDom().clientWidth
  const h = chart.getDom().clientHeight
  const from = chart.convertFromPixel({ seriesIndex: 0 }, [w / 2, h / 2])
  const to = [tx, ty]
  if (!from) return
  const t0 = performance.now()
  const dur = 350
  const ease = (t: number) => 1 - (1 - t) ** 3
  let lastK = 0
  const tick = () => {
    if (!chart) return
    const t = Math.min(1, (performance.now() - t0) / dur)
    const k = ease(t)
    // 本帧目标中心 → 与上帧的差值换算成屏幕像素 pan 增量
    const cx = from[0] + (to[0] - from[0]) * k
    const cy = from[1] + (to[1] - from[1]) * k
    const lx = from[0] + (to[0] - from[0]) * lastK
    const ly = from[1] + (to[1] - from[1]) * lastK
    lastK = k
    const pCur = chart.convertToPixel({ seriesIndex: 0 }, [lx, ly])
    const pNxt = chart.convertToPixel({ seriesIndex: 0 }, [cx, cy])
    if (!pCur || !pNxt) return
    // pan dx/dy 让内容跟随视图中心移动（方向与中心增量相反）
    chart.dispatchAction({
      type: 'graphRoam',
      dx: pCur[0] - pNxt[0],
      dy: pCur[1] - pNxt[1],
    } as any)
    if (t < 1) centerAnimRaf = requestAnimationFrame(tick)
  }
  tick()
}

function gotoChar(key: string) {
  // 定位到角色：以它为中心（保持缩放）；抽屉不关闭，
  // 直接换成目标角色的详情（高亮随动）
  const p = layoutPos.get(key)
  if (p) animateCenterTo(p[0], p[1])
  openCharCard(key)
}

// ---- 边详情 / 编辑 ----
const drawerOpen = ref(false)
const editing = ref<Relation | null>(null)
const editForm = ref({
  relation: '', category: 'other', polarity: '', description: '',
})

async function saveEdit() {
  if (!editing.value) return
  try {
    await api.post('/api/current/graph/relations/edge', {
      id: editing.value.id,
      source_var: editing.value.source_var,
      target_var: editing.value.target_var,
      ...editForm.value,
    })
    message.success('关系已保存')
    drawerOpen.value = false
    await load()
  } catch (e) {
    toastError(message, e)
  }
}

async function removeEdge() {
  if (!editing.value) return
  try {
    await api.del(`/api/current/graph/relations/edge/${editing.value.id}`)
    message.success('关系已删除')
    drawerOpen.value = false
    await load()
  } catch (e) {
    toastError(message, e)
  }
}

// ---- 新增关系 ----
const addOpen = ref(false)
const addForm = ref({
  source: '', target: '', relation: '', category: 'other', description: '',
})
const charOptions = computed(() =>
  (data.value?.characters ?? []).map((c) => ({
    label: `${c.cn_name || c.display_name}（${c.display_name}）`,
    value: c.key,
  })))

async function saveAdd() {
  if (!addForm.value.source || !addForm.value.target) {
    message.warning('请选择两个角色')
    return
  }
  if (addForm.value.source === addForm.value.target) {
    message.warning('不能选择同一个角色')
    return
  }
  try {
    await api.post('/api/current/graph/relations/edge', {
      source_var: addForm.value.source,
      target_var: addForm.value.target,
      relation: addForm.value.relation,
      category: addForm.value.category,
      description: addForm.value.description,
    })
    message.success('关系已添加')
    addOpen.value = false
    addForm.value = {
      source: '', target: '', relation: '', category: 'other', description: '',
    }
    await load()
  } catch (e) {
    toastError(message, e)
  }
}
</script>

<template>
  <div class="page">
    <n-card size="small">
      <n-space align="center" justify="space-between" style="width: 100%">
        <n-space align="center">
          <n-text strong style="font-size: 15px">人物关系图谱</n-text>
          <n-tag size="small" type="warning">实验功能</n-tag>
          <n-text v-if="data" depth="3" style="font-size: 12px">
            {{ visibleRelations.length }} / {{ data.relations.length }} 条关系
          </n-text>
          <n-space :size="4">
            <span v-for="(m, cat) in CATEGORY_META" :key="cat"
                  class="legend-chip">
              <span class="dot" :style="{ background: m.color }" />{{ m.label }}
            </span>
          </n-space>
        </n-space>
        <n-space>
          <n-switch v-model:value="showCooccur" size="small">
            <template #checked>共现</template>
            <template #unchecked>共现</template>
          </n-switch>
          <n-space size="small" align="center">
            <n-switch v-model:value="engineRender" size="small" />
            <n-text depth="3" style="font-size: 12px">引擎渲染（实验）</n-text>
          </n-space>
          <n-button size="small" :render-icon="renderIcon(AddOutline)"
                    :disabled="!data?.characters.length" @click="addOpen = true">
            添加关系
          </n-button>
          <n-popconfirm @positive-click="build">
            <template #trigger>
              <n-button size="small" :render-icon="renderIcon(RefreshOutline)">
                {{ data?.relations.length ? 'AI 重新生成' : 'AI 生成关系图谱' }}
              </n-button>
            </template>
            AI 重算会替换自动生成的关系（人工添加的保留），继续？
          </n-popconfirm>
        </n-space>
      </n-space>
      <n-space v-if="factions.length" :size="6" style="margin-top: 8px">
        <n-text depth="3" style="font-size: 12px">阵营：</n-text>
        <n-tag v-for="[f, n] in factions" :key="f" size="small"
               :color="{ borderColor: factionColor(f) }"
               :checked="factionFilter === f" checkable
               @click="highlightFaction(f)">
          {{ f }}（{{ n }}）
        </n-tag>
      </n-space>
    </n-card>

    <div class="body">
      <div v-if="loading" class="empty-wrap"><n-spin size="large" /></div>
      <div v-else-if="!hasGraph" class="empty-wrap">
        <n-empty description="尚未生成人物关系图谱">
          <template #extra>
            <n-button type="primary" :render-icon="renderIcon(PlayOutline)"
                      @click="build">
              AI 生成关系图谱
            </n-button>
          </template>
        </n-empty>
      </div>
      <div v-show="hasGraph" ref="container" class="flow" />
      <div v-show="hasGraph" ref="minimapEl" class="minimap" />
    </div>

    <!-- 角色卡抽屉 -->
    <n-drawer v-model:show="cardOpen" :width="360">
      <n-drawer-content v-if="cardChar" closable
                        :title="cardChar.cn_name || cardChar.display_name">
        <n-space vertical size="medium">
          <div style="text-align: center">
            <img v-if="cardChar.has_avatar"
                 :src="`/api/current/graph/avatar/${encodeURIComponent(cardChar.key)}?v=${avatarVer}`"
                 class="card-avatar" :alt="cardChar.display_name" />
            <div v-if="avatarCands(cardChar).length" style="margin-top: 8px">
              <n-button size="tiny" @click="openAvatarPicker">
                更换立绘
              </n-button>
            </div>
          </div>
          <n-space>
            <n-tag v-if="cardChar.key === protagonistKey" type="warning"
                   size="small">主角</n-tag>
            <n-tag v-if="cardChar.faction" size="small"
                   :color="{ borderColor: factionColor(cardChar.faction) }">
              {{ cardChar.faction }}
            </n-tag>
            <n-tag size="small">{{ cardChar.lines }} 句台词</n-tag>
            <n-tag size="small" type="default">{{ cardChar.display_name }}</n-tag>
          </n-space>
          <template v-if="cardProfile">
            <div v-for="(v, k) in cardProfile" :key="k">
              <n-text strong style="font-size: 13px">{{ k }}</n-text>
              <p style="margin: 3px 0; font-size: 12px; white-space: pre-wrap">
                {{ v }}
              </p>
            </div>
          </template>
          <n-text v-else depth="3" style="font-size: 12px">
            （尚未生成 AI 档案，可在「人名翻译」页分析）
          </n-text>
          <div v-if="cardRelations.length">
            <n-text strong style="font-size: 13px">
              关系（{{ cardRelations.length }}）
            </n-text>
            <div v-for="r in cardRelations" :key="r.id" class="rel-row">
              <span class="dot"
                    :style="{ background: (CATEGORY_META[r.category] ?? CATEGORY_META.other).color }" />
              <span class="rel-text">
                {{ r.relation || '（未命名）' }} ·
                {{ charName(r.source_var === cardChar.key
                  ? r.target_var : r.source_var) }}
              </span>
              <n-button text size="tiny" type="primary"
                        @click="gotoChar(r.source_var === cardChar!.key
                          ? r.target_var : r.source_var)">
                定位
              </n-button>
            </div>
          </div>
        </n-space>
      </n-drawer-content>
    </n-drawer>

    <!-- 立绘候选选择（随用随调，关闭即销毁） -->
    <n-modal v-model:show="avatarPickerOpen" preset="card" title="更换立绘"
             style="width: 560px">
      <div class="avatar-grid picker">
        <img v-for="p in pagedAvatars" :key="p"
             :src="`/api/current/graph/avatar_circle/${encodeURIComponent(cardChar?.key || '')}?path=${encodeURIComponent(p)}`"
             loading="lazy" class="avatar-cand"
             :class="{ active: p === pickAvatar }"
             alt="候选"
             @click="pickAvatar = p" />
      </div>
      <n-space justify="space-between" align="center" style="margin-top: 12px">
        <n-pagination v-if="avatarTotal > 12"
                      v-model:page="avatarPage" :page-size="12"
                      :item-count="avatarTotal" />
        <n-space justify="end" style="flex: 1">
          <n-button @click="avatarPickerOpen = false">取消</n-button>
          <n-button type="primary" :disabled="!pickAvatar"
                    @click="applyAvatarPref">
            确定
          </n-button>
        </n-space>
      </n-space>
    </n-modal>

    <!-- 边编辑抽屉 -->
    <n-drawer v-model:show="drawerOpen" :width="340">
      <n-drawer-content v-if="editing" closable
                        :title="`${charName(editing.source_var)} ↔ ${charName(editing.target_var)}`">
        <n-space vertical size="medium">
          <n-space>
            <n-tag size="small"
                   :type="editing.source === 'manual' ? 'success'
                     : editing.source === 'ai' ? 'info' : 'default'">
              {{ editing.source === 'manual' ? '人工' :
                 editing.source === 'ai' ? 'AI 推断' : '共现统计' }}
            </n-tag>
            <n-tag v-if="editing.cooccurrence" size="small">
              共同出场 {{ editing.cooccurrence }} 次
            </n-tag>
          </n-space>
          <n-form label-placement="top" size="small">
            <n-form-item label="关系">
              <n-input v-model:value="editForm.relation"
                       placeholder="如：母女、师生、恋人" />
            </n-form-item>
            <n-form-item label="类别">
              <n-select v-model:value="editForm.category"
                        :options="CATEGORY_OPTIONS" />
            </n-form-item>
            <n-form-item label="极性">
              <n-select v-model:value="editForm.polarity" clearable
                        :options="[
                          { label: '亲近/友善', value: 'positive' },
                          { label: '敌视/厌恶', value: 'negative' },
                          { label: '复杂/亦敌亦友', value: 'mixed' },
                        ]" />
            </n-form-item>
            <n-form-item label="描述">
              <n-input v-model:value="editForm.description" type="textarea"
                       :rows="4" placeholder="一句话关系描述" />
            </n-form-item>
          </n-form>
          <n-space>
            <n-button type="primary" size="small" @click="saveEdit">
              保存（转为人工）
            </n-button>
            <n-popconfirm @positive-click="removeEdge">
              <template #trigger>
                <n-button size="small" type="error" quaternary>删除</n-button>
              </template>
              删除这条关系？
            </n-popconfirm>
          </n-space>
        </n-space>
      </n-drawer-content>
    </n-drawer>

    <n-modal v-model:show="addOpen" preset="card" title="添加关系"
             style="width: 440px">
      <n-form label-placement="top" size="small">
        <n-form-item label="角色 A">
          <n-select v-model:value="addForm.source" :options="charOptions"
                    filterable placeholder="选择角色" />
        </n-form-item>
        <n-form-item label="角色 B">
          <n-select v-model:value="addForm.target" :options="charOptions"
                    filterable placeholder="选择角色" />
        </n-form-item>
        <n-form-item label="关系">
          <n-input v-model:value="addForm.relation"
                   placeholder="如：母女、师生、恋人" />
        </n-form-item>
        <n-form-item label="类别">
          <n-select v-model:value="addForm.category"
                    :options="CATEGORY_OPTIONS" />
        </n-form-item>
        <n-form-item label="描述">
          <n-input v-model:value="addForm.description" type="textarea"
                   :rows="3" placeholder="一句话关系描述（可选）" />
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button size="small" @click="addOpen = false">取消</n-button>
          <n-button size="small" type="primary" @click="saveAdd">添加</n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 10px;
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
.legend-chip {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 11px;
  color: rgba(255,255,255,0.45);
}
.dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.card-avatar {
  width: 140px;
  height: 140px;
  object-fit: cover;
  object-position: top;
  border-radius: 8px;
  background: #2a2a30;
}
.avatar-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 6px;
  margin-top: 6px;
}
.avatar-cand {
  width: 100%;
  aspect-ratio: 1;
  object-fit: cover;
  object-position: top;
  border-radius: 6px;
  border: 2px solid transparent;
  cursor: pointer;
  background: #1c1c20;
}
.avatar-cand.active {
  border-color: var(--gt-primary, #34d399);
}
.rel-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 5px;
  font-size: 12px;
}
.rel-text { flex: 1; }
</style>
