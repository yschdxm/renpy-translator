<script setup lang="ts">
/** 文件选择拖拽区：n-upload + dragger 的封装——只选不传（:default-upload="false"），
 *  由调用方在提交时上传。v-model 绑定选中的 File。 */
import { NText, NUpload, NUploadDragger } from 'naive-ui'
import type { UploadFileInfo } from 'naive-ui'

defineProps<{
  modelValue: File | null
  accept?: string
  placeholder?: string
}>()
const emit = defineEmits<{ 'update:modelValue': [File | null] }>()

function onChange(o: { fileList: UploadFileInfo[] }) {
  emit('update:modelValue', o.fileList[0]?.file ?? null)
}
</script>

<template>
  <n-upload :default-upload="false" :show-file-list="false" :max="1"
            :accept="accept" @change="onChange">
    <n-upload-dragger>
      <div style="padding: 12px">
        <n-text>{{ modelValue?.name || placeholder || '点击或拖拽选择文件' }}</n-text>
      </div>
    </n-upload-dragger>
  </n-upload>
</template>
