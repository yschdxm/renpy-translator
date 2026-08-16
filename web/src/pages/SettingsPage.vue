<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import {
  NButton, NCard, NEmpty, NForm, NFormItem, NInput, NInputGroup, NInputNumber,
  NModal, NPopconfirm, NSpace, NSpin, NTag, NText, useMessage,
} from 'naive-ui'
import { AddOutline, CloudDownloadOutline, FolderOpenOutline } from '@vicons/ionicons5'
import { api, toastError, toastOk } from '../api/client'
import { renderIcon } from '../components/icons'
import { nativeReady, pickDirectory } from '../api/native'
import { useJobTask } from '../composables/useJobTask'
import { useSessionStore } from '../stores/session'

const message = useMessage()
const session = useSessionStore()
const { runJob } = useJobTask()

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
  is_active?: boolean
}

const configs = ref<ModelConfig[]>([])
const sdkPath8 = ref('')
const sdkPath7 = ref('')
const sdkVersion = ref('')
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
    toastOk(message, `数据目录已迁移（${result.moved.length} 项）`)
    if (result.leftover?.length) {
      message.info(
        `${result.leftover.length} 个被占用的日志文件已复制到新目录，`
        + '服务将自动重启完成清理，恢复后页面会自动刷新',
        { duration: 12000, closable: true })
    }
  } catch (e) {
    toastError(message, e)
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
  const settings = await api.get<{
    sdk_path_8: string; sdk_path_7: string; data_dir: string
  }>('/api/settings')
  sdkPath8.value = settings.sdk_path_8
  sdkPath7.value = settings.sdk_path_7
  dataDir.value = settings.data_dir
  if (!newDataDir.value) newDataDir.value = settings.data_dir
}

function openCreate() {
  Object.assign(form, emptyForm())
  formEditing.value = ''
  formVisible.value = true
}

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

// ---- SDK 下载（任务） ----
async function downloadSdk(version: string) {
  await runJob(
    () => api.post('/api/settings/sdk/download', { version }),
    async (status) => {
      if (status === 'succeeded') {
        await refresh()
        toastOk(message, 'SDK 下载完成')
      }
    })
}

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
        <n-text depth="3" style="font-size: 12px">
          需要 7.x 与 8.x 两个版本：创建项目与导出校验会按游戏引擎自动选择；
          缺失的大版本会在启动时自动下载补齐。SDK 固定安装在数据目录的 tools/ 下。
        </n-text>
        <n-space align="center">
          <n-tag size="small" :type="sdkPath8 ? 'success' : 'default'"
                 style="width: 66px; justify-content: center">Ren'Py 8</n-tag>
          <n-text style="font-size: 13px">{{ sdkPath8 || '未安装' }}</n-text>
          <n-button size="small" :render-icon="renderIcon(CloudDownloadOutline)" @click="downloadSdk('8.5.3')">
            {{ sdkPath8 ? '重新下载 8.5.3' : '下载 8.5.3' }}
          </n-button>
        </n-space>
        <n-space align="center">
          <n-tag size="small" :type="sdkPath7 ? 'success' : 'default'"
                 style="width: 66px; justify-content: center">Ren'Py 7</n-tag>
          <n-text style="font-size: 13px">{{ sdkPath7 || '未安装' }}</n-text>
          <n-button size="small" :render-icon="renderIcon(CloudDownloadOutline)" @click="downloadSdk('7.4.11')">
            {{ sdkPath7 ? '重新下载 7.4.11' : '下载 7.4.11' }}
          </n-button>
        </n-space>
        <n-space align="center">
          <n-text depth="3" style="font-size: 12px">其他版本：</n-text>
          <n-input v-model:value="sdkVersion" size="small" style="width: 90px" placeholder="如 8.4.1" />
          <n-button size="small" :disabled="!sdkVersion.trim()" @click="downloadSdk(sdkVersion.trim())">
            下载
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
      <n-text depth="3" style="font-size: 12px">已激活的配置对所有项目全局生效，保存后立即应用</n-text>
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
          <n-input-number v-model:value="form.max_tokens"
                          placeholder="单次回复的输出上限（非上下文窗口）" style="width: 100%" />
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
