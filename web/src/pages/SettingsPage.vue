<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import {
  NButton, NCard, NEmpty, NForm, NFormItem, NInput, NInputGroup, NInputNumber,
  NModal, NPopconfirm, NSpace, NSpin, NTag, NText, useMessage,
} from 'naive-ui'
import { AddOutline, CloudDownloadOutline, FolderOpenOutline } from '@vicons/ionicons5'
import { api, errorText } from '../api/client'
import { renderIcon } from '../components/icons'
import { nativeReady, pickDirectory } from '../api/native'
import { useJobsStore } from '../stores/jobs'
import { useSessionStore } from '../stores/session'

const message = useMessage()
const jobsStore = useJobsStore()
const session = useSessionStore()

interface ModelConfig {
  name: string
  api_base: string
  api_key: string
  model: string
  temperature: number
  max_tokens: number
  context_lines: number
  timeout: number
  max_context: number
  batch_lines: number
}

const configs = ref<ModelConfig[]>([])
const sdkPath = ref('')
const sdkVersion = ref('8.5.3')
const sdkTesting = ref(false)
const guiMode = ref(false)

// ---- 数据目录 ----
const dataDir = ref('')
const newDataDir = ref('')
const migrating = ref(false)

async function browseDataDir() {
  const dir = await pickDirectory()
  if (dir) newDataDir.value = dir
}

async function migrateDataDir() {
  if (!newDataDir.value.trim() || newDataDir.value.trim() === dataDir.value) {
    message.warning('请输入与当前不同的新目录')
    return
  }
  migrating.value = true
  try {
    const result = await api.put<{
      home: string; moved: string[]
      leftover?: string[]; leftover_dir?: string; restarting?: boolean
    }>('/api/settings/data-dir', { path: newDataDir.value.trim() })
    dataDir.value = result.home
    newDataDir.value = result.home
    await session.refresh()
    message.success(`数据目录已迁移（${result.moved.length} 项）`)
    if (result.leftover?.length) {
      message.info(
        `${result.leftover.length} 个被占用的日志文件已复制到新目录，`
        + '服务将自动重启完成清理，恢复后页面会自动刷新',
        { duration: 12000, closable: true })
    }
  } catch (e) {
    message.error(errorText(e), { duration: 12000, closable: true })
  } finally {
    migrating.value = false
  }
}

const emptyForm = (): ModelConfig => ({
  name: '', api_base: 'https://api.openai.com/v1', api_key: '',
  model: '', temperature: 0.3, max_tokens: 1000, context_lines: 3,
  timeout: 30, max_context: 8, batch_lines: 100,
})

const formVisible = ref(false)
const formEditing = ref('')   // 非空 = 编辑已有配置
const form = reactive<ModelConfig>(emptyForm())
const testing = ref(false)

async function refresh() {
  configs.value = await api.get<ModelConfig[]>('/api/configs')
  const settings = await api.get<{ sdk_path: string; data_dir: string }>('/api/settings')
  sdkPath.value = settings.sdk_path
  dataDir.value = settings.data_dir
  if (!newDataDir.value) newDataDir.value = settings.data_dir
}

function openCreate() {
  Object.assign(form, emptyForm())
  formEditing.value = ''
  formVisible.value = true
}

function openEdit(c: ModelConfig) {
  Object.assign(form, c)
  formEditing.value = c.name
  formVisible.value = true
}

async function saveForm() {
  try {
    if (formEditing.value) {
      await api.put(`/api/configs/${encodeURIComponent(formEditing.value)}`, { ...form })
    } else {
      await api.post('/api/configs', { ...form })
    }
    formVisible.value = false
    message.success('配置已保存')
    await refresh()
  } catch (e) {
    message.error(errorText(e), { duration: 8000 })
  }
}

async function removeConfig(c: ModelConfig) {
  try {
    await api.del(`/api/configs/${encodeURIComponent(c.name)}`)
    message.success('配置已删除')
    await refresh()
  } catch (e) {
    message.error(errorText(e), { duration: 8000 })
  }
}

async function testConnection() {
  testing.value = true
  try {
    const result = await api.post<{ model: string; response: string }>(
      '/api/configs/test', { ...form })
    message.success(`连接成功（${result.model}）`)
  } catch (e) {
    message.error(errorText(e), { duration: 10000, closable: true })
  } finally {
    testing.value = false
  }
}

async function saveSdkPath() {
  await api.put('/api/settings', { sdk_path: sdkPath.value })
  message.success('SDK 路径已保存')
}

async function autoFindSdk() {
  try {
    const result = await api.post<{ sdk_path: string }>('/api/settings/sdk/find')
    sdkPath.value = result.sdk_path
    message.success(`找到 SDK: ${result.sdk_path}`)
    await saveSdkPath()
  } catch (e) {
    message.error(errorText(e), { duration: 8000 })
  }
}

async function testSdk() {
  sdkTesting.value = true
  try {
    await api.post('/api/settings/sdk/test', { path: sdkPath.value })
    message.success('SDK 路径有效')
    await saveSdkPath()
  } catch (e) {
    message.error(errorText(e), { duration: 8000 })
  } finally {
    sdkTesting.value = false
  }
}

// ---- SDK 下载（任务） ----
const sdkJobId = ref('')

async function downloadSdk() {
  try {
    const data = await api.post<{ job_id: string }>(
      '/api/settings/sdk/download', { version: sdkVersion.value })
    sdkJobId.value = data.job_id
    jobsStore.track(data.job_id)
  } catch (e) {
    message.error(errorText(e), { duration: 10000, closable: true })
  }
}

watch(
  () => sdkJobId.value && jobsStore.jobs.get(sdkJobId.value)?.status,
  async (status) => {
    if (status === 'succeeded') {
      await refresh()
      message.success('SDK 下载完成')
    }
  })

onMounted(async () => {
  guiMode.value = await nativeReady()
  await refresh()
})
</script>

<template>
  <div style="max-width: 860px">
    <h2 style="margin-top: 0">模型配置</h2>

    <n-card size="small" title="Ren'Py SDK" style="margin-bottom: 16px">
      <n-space vertical>
        <n-space align="center">
          <n-input v-model:value="sdkPath" placeholder="SDK 路径（如 D:\renpy-8.5.3-sdk）" style="width: 420px" />
          <n-button size="small" @click="autoFindSdk">自动查找</n-button>
          <n-button size="small" :loading="sdkTesting" @click="testSdk">测试并保存</n-button>
        </n-space>
        <n-space align="center">
          <n-text depth="3" style="font-size: 12px">没有 SDK？输入版本号一键下载：</n-text>
          <n-input v-model:value="sdkVersion" size="small" style="width: 90px" placeholder="8.5.3" />
          <n-button size="small" type="primary" :render-icon="renderIcon(CloudDownloadOutline)" @click="downloadSdk">
            下载 SDK
          </n-button>
        </n-space>
      </n-space>
    </n-card>

    <n-card size="small" title="数据目录" style="margin-bottom: 16px">
      <n-space vertical>
        <n-text depth="3" style="font-size: 12px">
          项目、配置、日志、导出等全部数据的位置。修改后会把现有数据迁移过去（迁移期间请勿操作）。
        </n-text>
        <n-text style="font-size: 13px">当前：{{ dataDir }}</n-text>
        <n-input-group style="width: 560px">
          <n-input v-model:value="newDataDir" placeholder="新数据目录" />
          <n-button v-if="guiMode" size="small" :render-icon="renderIcon(FolderOpenOutline)" @click="browseDataDir">
            浏览
          </n-button>
          <n-popconfirm @positive-click="migrateDataDir">
            <template #trigger>
              <n-button size="small" type="warning" :disabled="migrating">迁移并切换</n-button>
            </template>
            将把 {{ dataDir }} 的全部数据迁移到 {{ newDataDir }}，确定？
          </n-popconfirm>
        </n-input-group>
      </n-space>
    </n-card>

    <n-space align="center" style="margin-bottom: 10px">
      <h3 style="margin: 0">AI 模型</h3>
      <n-button size="small" type="primary" :render-icon="renderIcon(AddOutline)" @click="openCreate">新建配置</n-button>
    </n-space>

    <n-empty v-if="!configs.length" description="暂无模型配置" style="margin: 40px 0" />

    <n-card v-for="c in configs" :key="c.name" size="small" style="margin-bottom: 8px">
      <n-space align="center" justify="space-between">
        <n-space align="center">
          <n-text strong>{{ c.name }}</n-text>
          <n-tag size="small" :bordered="false">{{ c.model }}</n-tag>
          <n-text depth="3" style="font-size: 12px">{{ c.api_base }}</n-text>
        </n-space>
        <n-space>
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
             :title="formEditing ? '编辑配置' : '新建配置'" style="width: 560px">
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
          <n-input-number v-model:value="form.temperature" :step="0.05" />
        </n-form-item>
        <n-form-item label="max_tokens">
          <n-input-number v-model:value="form.max_tokens" />
        </n-form-item>
        <n-form-item label="上下文行数">
          <n-input-number v-model:value="form.context_lines" />
        </n-form-item>
        <n-form-item label="上下文窗口(K)">
          <n-input-number v-model:value="form.max_context" />
        </n-form-item>
        <n-form-item label="每批句数">
          <n-input-number v-model:value="form.batch_lines" />
        </n-form-item>
        <n-form-item label="超时(秒)">
          <n-input-number v-model:value="form.timeout" />
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button :loading="testing" @click="testConnection">测试连接</n-button>
          <n-button @click="formVisible = false">取消</n-button>
          <n-button type="primary" @click="saveForm">保存</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- 迁移中阻塞模态 -->
    <n-modal :show="migrating" preset="card" title="正在迁移数据目录" style="width: 380px"
             :mask-closable="false" :closable="false">
      <n-space align="center">
        <n-spin size="small" />
        <n-text>正在迁移全部数据，请勿关闭应用...</n-text>
      </n-space>
    </n-modal>
  </div>
</template>
