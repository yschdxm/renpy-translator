/** 页面状态保留（keep-alive）配套：滚动位置保存/恢复 + 激活时后台刷新
 *
 * 滚动容器是 AppInner 共享的 n-layout-content（非 window），按路由 path
 * 分别记录；激活时先恢复位置，再按当前查询条件后台刷新，刷新后二次恢复
 * 防止表格重绘把位置顶走。状态保留≠数据陈旧：页码/筛选不动，数据更新。
 */
import { inject, nextTick, onActivated, onDeactivated } from 'vue'
import type { InjectionKey } from 'vue'
import { useRoute } from 'vue-router'

const positions = new Map<string, number>()

export const SCROLL_EL_KEY: InjectionKey<() => HTMLElement | null> =
  Symbol('rtScrollEl')

/** 路由离开前（DOM 未变）记录滚动位置。
 *  不能在 onDeactivated 里存：那时新页面已挂载，共享容器的 scrollTop
 *  可能已被较短的新内容钳成 0。 */
export function recordScrollPosition(path: string, top: number) {
  positions.set(path, top)
}

/** 项目切换时调用：旧项目的滚动位置全部作废 */
export function clearPageScrollPositions() {
  positions.clear()
}

export function usePageScroll(refresh?: () => Promise<unknown> | void) {
  const route = useRoute()
  const getEl = inject(SCROLL_EL_KEY, () => null)
  let wasDeactivated = false

  function restore(saved: number | undefined) {
    const el = getEl()
    if (el && saved !== undefined) el.scrollTop = saved
  }

  onDeactivated(() => {
    wasDeactivated = true
  })

  onActivated(async () => {
    if (!wasDeactivated) return  // 首次挂载：onMounted 已加载数据
    wasDeactivated = false
    const saved = positions.get(route.path)
    await nextTick()
    restore(saved)
    if (refresh) {
      // 刷新失败保留缓存数据与页码（各页 load 自报错误），不重置页面
      await Promise.resolve(refresh()).catch(() => {})
      await nextTick()
      restore(saved)
    }
  })
}
