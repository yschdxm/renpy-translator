/** ECharts 公共初始化 + 小地图组件
 *  两个图页面共用，保证交互与视觉一致 */
import * as echarts from 'echarts'
import { GT } from './graph-theme'

/** 初始化 chart：暗色默认 + 容器尺寸变化自动 resize（echarts 原生
 *  HiDPI/resize 管线，不需要额外 hack）。dispose 时自动断开观察器 */
export function makeChart(el: HTMLElement): echarts.ECharts {
  const chart = echarts.init(el, undefined, { renderer: 'canvas' })
  const ro = new ResizeObserver(() => {
    if (!chart.isDisposed()) chart.resize()
  })
  ro.observe(el)
  const dispose = chart.dispose.bind(chart)
  chart.dispose = () => {
    ro.disconnect()
    dispose()
  }
  return chart
}

export interface MinimapNode {
  x: number
  y: number
  color: string
  r: number
}

/** 小地图：简化圆点总览 + 视口框 + 点击定位。
 *  主图 roam/dataZoom 时调 setView 同步视口框（数据坐标） */
export class MiniMap {
  private chart: echarts.ECharts
  private disposed = false

  constructor(el: HTMLElement, onPick: (x: number, y: number) => void) {
    this.chart = makeChart(el)
    this.chart.on('click', (p) => {
      const evt = (p as any).event
      if (!evt) return
      const pt = this.chart.convertFromPixel(
        { xAxisIndex: 0, yAxisIndex: 0 },
        [evt.offsetX, evt.offsetY])
      if (pt) onPick(pt[0], pt[1])
    })
  }

  /** 更新总览节点（全量数据坐标，含边距适配到小地图画幅） */
  setNodes(nodes: MinimapNode[]) {
    if (this.disposed || !nodes.length) return
    const xs = nodes.map((n) => n.x)
    const ys = nodes.map((n) => n.y)
    let x0 = Math.min(...xs)
    let x1 = Math.max(...xs)
    let y0 = Math.min(...ys)
    let y1 = Math.max(...ys)
    // 等比适配：数据跨度不足时补出最小跨度，外加 12% 边距
    const rect = this.chart.getDom().getBoundingClientRect()
    const aspect = rect.width / Math.max(rect.height, 1)
    const spanX = Math.max(x1 - x0, 100)
    const spanY = Math.max(y1 - y0, 100)
    const scale = Math.max(spanX / aspect, spanY)
    const pad = scale * 0.12
    const cx = (x0 + x1) / 2
    const cy = (y0 + y1) / 2
    x0 = cx - (scale * aspect) / 2 - pad
    x1 = cx + (scale * aspect) / 2 + pad
    y0 = cy - scale / 2 - pad
    y1 = cy + scale / 2 + pad
    this.chart.setOption({
      animation: false,
      grid: { left: 0, right: 0, top: 0, bottom: 0 },
      xAxis: { show: false, min: x0, max: x1 },
      yAxis: { show: false, min: y0, max: y1, inverse: true },
      series: [{
        type: 'scatter',
        silent: true,
        data: nodes.map((n) => ({
          value: [n.x, n.y],
          symbolSize: n.r,
          itemStyle: { color: n.color, opacity: 0.9 },
        })),
      }],
    }, { replaceMerge: ['series'] })
  }

  /** 视口框（数据坐标矩形，内部转像素绘制） */
  setView(x0: number, x1: number, y0: number, y1: number) {
    if (this.disposed) return
    const p0 = this.chart.convertToPixel(
      { xAxisIndex: 0, yAxisIndex: 0 }, [x0, y0])
    const p1 = this.chart.convertToPixel(
      { xAxisIndex: 0, yAxisIndex: 0 }, [x1, y1])
    if (!p0 || !p1) return
    this.chart.setOption({
      graphic: [{
        type: 'rect',
        silent: true,
        position: [p0[0], p0[1]],
        shape: {
          x: 0, y: 0,
          width: Math.max(p1[0] - p0[0], 4),
          height: Math.max(p1[1] - p0[1], 4),
        },
        style: {
          fill: 'rgba(255,255,255,0.07)',
          stroke: GT.border,
          lineWidth: 1,
        },
      }],
    })
  }

  dispose() {
    this.disposed = true
    this.chart.dispose()
  }
}
