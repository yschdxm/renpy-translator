/** 图标渲染助手：<n-button :render-icon="renderIcon(Xxx)"> */
import { h, type Component } from 'vue'
import { NIcon } from 'naive-ui'

export function renderIcon(icon: Component) {
  return () => h(NIcon, null, { default: () => h(icon) })
}
