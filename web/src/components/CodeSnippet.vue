<script setup lang="ts">
/** 候选处源码查看：前后 6 行，目标行底色 + 候选字面量高亮 */
import { onMounted, ref } from 'vue'
import { NSpin } from 'naive-ui'
import { api, errorText } from '../api/client'

const props = defineProps<{ file: string; line: number; literal: string }>()

interface SnippetLine { no: number; text: string; is_target: boolean }

const lines = ref<SnippetLine[]>([])
const error = ref('')
const loading = ref(true)

onMounted(async () => {
  try {
    const data = await api.get<{ lines: SnippetLine[] }>(
      `/api/current/embedded/snippet?file=${encodeURIComponent(props.file)}&line=${props.line}&ctx=6`)
    lines.value = data.lines
  } catch (e) {
    error.value = errorText(e)
  } finally {
    loading.value = false
  }
})

/** 把目标行拆成 [前, 高亮, 后]（literal 含引号，原样匹配） */
function segments(text: string, isTarget: boolean): Array<{ text: string; mark: boolean }> {
  if (!isTarget || !props.literal) return [{ text, mark: false }]
  const idx = text.indexOf(props.literal)
  if (idx === -1) return [{ text, mark: false }]
  return [
    { text: text.slice(0, idx), mark: false },
    { text: props.literal, mark: true },
    { text: text.slice(idx + props.literal.length), mark: false },
  ].filter((s) => s.text)
}
</script>

<template>
  <div style="margin: 6px 0 6px 24px; background: #141414; border-radius: 6px; padding: 8px; font-family: monospace; font-size: 12px">
    <n-spin v-if="loading" size="small" />
    <div v-else-if="error" style="color: #e88080">{{ error }}</div>
    <div v-else>
      <div
        v-for="l in lines" :key="l.no"
        :style="l.is_target ? 'background: #4a4420; padding: 0 4px' : 'padding: 0 4px; color: #999'"
      >
        <span style="color: #555; margin-right: 8px">{{ l.no }}</span>
        <span
          v-for="(seg, i) in segments(l.text, l.is_target)" :key="i"
          :style="seg.mark ? 'background: #ffca28; color: #000; border-radius: 2px' : ''"
        >{{ seg.text }}</span>
      </div>
    </div>
  </div>
</template>
