<script setup lang="ts">
/** 模型配置页：仅 AI 模型（SDK/数据目录在路径配置，MCP 在 MCP 接入） */
import { onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NButton, NCard, NEmpty, NForm, NFormItem, NInput, NInputNumber,
  NModal, NPopconfirm, NSelect, NSpace, NTag, NText, useMessage,
} from 'naive-ui'
import { AddOutline, RefreshOutline } from '@vicons/ionicons5'
import { api, toastError, toastOk } from '../api/client'
import { renderIcon } from '../components/icons'
import PageHeader from '../components/PageHeader.vue'
import { usePageScroll } from '../composables/usePageScroll'

const message = useMessage()
const route = useRoute()
const router = useRouter()

interface ModelConfig {
  name: string
  api_base: string
  api_key: string
  model: string
  temperature: number | null
  max_tokens: number | null
  context_lines: number
  timeout: number
  max_context: number
  batch_lines: number
  thinking: string
  is_active?: boolean
}

const configs = ref<ModelConfig[]>([])

const emptyForm = (): ModelConfig => ({
  name: '', api_base: 'https://api.openai.com/v1', api_key: '',
  model: '', temperature: null, max_tokens: null, context_lines: 3,
  timeout: 30, max_context: 8, batch_lines: 100, thinking: 'default',
})

const formVisible = ref(false)
const formEditing = ref('')   // 非空 = 编辑已有配置
const form = reactive<ModelConfig>(emptyForm())
const testing = ref(false)

async function refresh() {
  configs.value = await api.get<ModelConfig[]>('/api/configs')
}

function openCreate() {
  Object.assign(form, emptyForm())
  formEditing.value = ''
  formVisible.value = true
}

// ?new=1：从「未配置模型」提示/首启卡直达新建配置弹窗
watch(() => route.query.new, (v) => {
  if (v === undefined) return
  openCreate()
  void router.replace({ query: {} })
}, { immediate: true })

async function openEdit(c: ModelConfig) {
  Object.assign(form, c)
  formEditing.value = c.name
  formVisible.value = true
  try {
    // 列表里的是掩码，编辑时回显真实 key
    const { api_key } = await api.get<{ api_key: string }>(
      `/api/configs/${encodeURIComponent(c.name)}/key`)
    form.api_key = api_key
  } catch (e) {
    toastError(message, e)
  }
}

async function saveForm() {
  try {
    if (formEditing.value) {
      await api.put(`/api/configs/${encodeURIComponent(formEditing.value)}`, { ...form })
    } else {
      await api.post('/api/configs', { ...form })
    }
    formVisible.value = false
    toastOk(message, '配置已保存')
    await refresh()
  } catch (e) {
    toastError(message, e)
  }
}

async function removeConfig(c: ModelConfig) {
  try {
    await api.del(`/api/configs/${encodeURIComponent(c.name)}`)
    toastOk(message, '配置已删除')
    await refresh()
  } catch (e) {
    toastError(message, e)
  }
}

async function activateConfig(c: ModelConfig) {
  try {
    await api.post(`/api/configs/${encodeURIComponent(c.name)}/activate`)
    toastOk(message, `已激活: ${c.name}`)
    await refresh()
  } catch (e) {
    toastError(message, e)
  }
}

async function testConnection() {
  testing.value = true
  try {
    const result = await api.post<{ model: string; response: string }>(
      '/api/configs/test', { ...form })
    toastOk(message, `连接成功（${result.model}）`)
  } catch (e) {
    toastError(message, e)
  } finally {
    testing.value = false
  }
}

onMounted(refresh)
usePageScroll(refresh)
</script>

<template>
  <div style="max-width: 860px">
    <page-header title="模型配置" help-section="1">
      <template #actions>
        <n-button size="small" :render-icon="renderIcon(RefreshOutline)" @click="refresh">
          刷新
        </n-button>
      </template>
    </page-header>

    <n-space align="center" style="margin-bottom: 10px">
      <n-button size="small" type="primary" :render-icon="renderIcon(AddOutline)"
                @click="openCreate">新建配置</n-button>
      <n-text depth="3" style="font-size: 12px">
        已激活的配置对所有项目全局生效，保存后立即应用。
        Ren'Py SDK 与数据目录见「路径配置」；AI 编程助手接入见「MCP 接入」。
      </n-text>
    </n-space>

    <n-empty v-if="!configs.length" description="暂无模型配置" style="margin: 40px 0" />

    <n-card v-for="c in configs" :key="c.name" size="small" style="margin-bottom: 8px">
      <n-space align="center" justify="space-between">
        <n-space align="center">
          <n-text strong>{{ c.name }}</n-text>
          <n-tag v-if="c.is_active" size="small" type="success">已激活</n-tag>
          <n-tag size="small" :bordered="false">{{ c.model }}</n-tag>
          <n-text depth="3" style="font-size: 12px">{{ c.api_base }}</n-text>
        </n-space>
        <n-space>
          <n-button v-if="!c.is_active" size="small" type="primary" secondary
                    @click="activateConfig(c)">激活</n-button>
          <n-button size="small" @click="openEdit(c)">编辑</n-button>
          <n-popconfirm @positive-click="removeConfig(c)">
            <template #trigger>
              <n-button size="small" type="error" quaternary>删除</n-button>
            </template>
            确定删除配置「{{ c.name }}」？
          </n-popconfirm>
        </n-space>
      </n-space>
    </n-card>

    <n-modal v-model:show="formVisible" preset="card"
             :title="formEditing ? '编辑配置' : '新建配置'" style="width: min(560px, 96vw)">
      <n-form label-placement="left" label-width="110">
        <n-form-item label="名称">
          <n-input v-model:value="form.name" :disabled="!!formEditing" />
        </n-form-item>
        <n-form-item label="API Base">
          <n-input v-model:value="form.api_base" />
        </n-form-item>
        <n-form-item label="API Key">
          <n-input v-model:value="form.api_key" type="password" show-password-on="click"
                   placeholder="sk-..." />
        </n-form-item>
        <n-form-item label="模型">
          <n-input v-model:value="form.model" placeholder="如 gpt-4o / deepseek-chat" />
        </n-form-item>
        <n-form-item label="温度">
          <n-input-number v-model:value="form.temperature" :step="0.05" clearable
                          placeholder="留空=模型默认（不发参数）" style="width: 100%" />
        </n-form-item>
        <n-form-item label="max_tokens">
          <n-input-number v-model:value="form.max_tokens" clearable
                          placeholder="留空=不设上限（推荐，不发参数，避免截断输出）" style="width: 100%" />
        </n-form-item>
        <n-form-item label="参考台词行数">
          <n-input-number v-model:value="form.context_lines" :min="0" :precision="0" />
        </n-form-item>
        <n-text depth="3" style="display: block; margin: -12px 0 16px; font-size: 12px">
          随待译文本附带的参考台词上限，0 为不附带。单句翻译取前后各这么多行；
          批量只取前文、最多 8 行，窗口不足时还会减少。参考台词仅帮助模型理解剧情，不会在本次请求中修改其译文。
        </n-text>
        <n-form-item label="上下文窗口(K)">
          <n-input-number v-model:value="form.max_context" :min="1" :precision="0" />
        </n-form-item>
        <n-text depth="3" style="display: block; margin: -12px 0 16px; font-size: 12px">
          单次请求的总容量（1K = 1024 tokens）：提示词、人名/术语、参考台词、待译文本和模型输出共用。
          使用 LM Studio 时按模型实际加载的上下文长度填写，例如 32768 tokens 填 32；
          此处不会改变模型服务的加载设置。
        </n-text>
        <n-form-item label="每批句数">
          <n-input-number v-model:value="form.batch_lines" :min="1" :precision="0" />
        </n-form-item>
        <n-text depth="3" style="display: block; margin: -12px 0 16px; font-size: 12px">
          一次批量请求最多翻译多少条，不是并发数。实际批次还受窗口和输出预算限制，长句会少于此值。
          调大可减少请求次数，但等待更久、漏译后的重试成本更高；漏译多时可调小。
        </n-text>
        <n-form-item label="超时(秒)">
          <n-input-number v-model:value="form.timeout" :min="1" :precision="0" />
        </n-form-item>
        <n-form-item label="思考模式">
          <n-select v-model:value="form.thinking" :options="[
            { label: '跟随模型默认（不发参数）', value: 'default' },
            { label: '开启', value: 'enabled' },
            { label: '关闭', value: 'disabled' },
          ]" />
        </n-form-item>
        <n-text depth="3" style="font-size: 12px; margin-top: -12px">
          思考模式向请求体发送 thinking 参数（DeepSeek V4 / GLM / 豆包等支持；
          DeepSeek V4 默认即思考，选「关闭」才会显式关掉）。接口不支持时
          「测试连接」会报 400，改回默认即可。思考内容占输出额度——
          max_tokens 留空（不设上限）时无需额外调整。
        </n-text>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button :loading="testing" @click="testConnection">测试连接</n-button>
          <n-button @click="formVisible = false">取消</n-button>
          <n-button type="primary" @click="saveForm">保存</n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>
