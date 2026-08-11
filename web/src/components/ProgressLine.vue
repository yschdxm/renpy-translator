<!-- 统一进度条组件。
     规范：
     - 任务级进度一律传 0-1（后端 emit_progress 约定）；
     - 统计级 0-100 的值传入前由调用方除以 100；
     - 高度统一 8px，不允许各页面自定义 height；
     - status 保持受控：不传即 'default'，不做 value>=1 自动 success。 -->
<script setup lang="ts">
import { computed } from 'vue'
import { NProgress } from 'naive-ui'

interface Props {
  /** 进度值 0-1（超出范围自动 clamp） */
  value: number
  /** 受控状态，不传为 'default' */
  status?: 'default' | 'success' | 'error' | 'warning'
  /** 进行中动画，透传给 n-progress */
  processing?: boolean
  /** 是否显示百分比文本（默认显示） */
  showText?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  status: 'default',
  processing: false,
  showText: true,
})

const percentage = computed(() => {
  const v = Math.min(1, Math.max(0, props.value)) // clamp 到 0-1
  return Math.round(v * 100)
})
</script>

<template>
  <!-- height 固定 8：全项目统一 8px 进度条 -->
  <NProgress
    type="line"
    :percentage="percentage"
    :status="status"
    :processing="processing"
    :show-indicator="showText"
    :height="8"
  />
</template>
