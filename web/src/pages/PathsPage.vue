<script setup lang="ts">
/** 路径配置页：Ren'Py SDK + 数据目录 */
import { onMounted, ref } from 'vue'
import {
  NButton, NCard, NInput, NInputGroup, NModal, NPopconfirm, NSpace, NSpin,
  NTag, NText, useMessage,
} from 'naive-ui'
import { CloudDownloadOutline, FolderOpenOutline, RefreshOutline } from '@vicons/ionicons5'
import { api, toastError, toastOk } from '../api/client'
import { renderIcon } from '../components/icons'
import PageHeader from '../components/PageHeader.vue'
import { nativeReady, pickDirectory } from '../api/native'
import { useJobTask } from '../composables/useJobTask'
import { usePageScroll } from '../composables/usePageScroll'
import { useSessionStore } from '../stores/session'

const message = useMessage()
const session = useSessionStore()
const { runJob } = useJobTask()

interface SdkInfo {
  version: string
  version_tuple: number[]
  path: string
  major: number
  is_default: boolean
}
const sdkList = ref<SdkInfo[]>([])
const sdkVersion = ref('')
const guiMode = ref(false)

// ---- 数据目录 ----
const dataDir = ref('')
const newDataDir = ref('')
const migrating = ref(false)

async function refresh() {
  const settings = await api.get<{ sdks: SdkInfo[]; data_dir: string }>(
    '/api/settings')
  sdkList.value = settings.sdks || []
  dataDir.value = settings.data_dir
  if (!newDataDir.value) newDataDir.value = settings.data_dir
}

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
usePageScroll(refresh)
</script>

<template>
  <div style="max-width: 860px">
    <page-header title="路径配置" help-section="2">
      <template #actions>
        <n-button size="small" :render-icon="renderIcon(RefreshOutline)" @click="refresh">
          刷新
        </n-button>
      </template>
    </page-header>

    <n-card size="small" title="Ren'Py SDK" style="margin-bottom: 16px">
      <n-space vertical>
        <n-text depth="3" style="font-size: 12px">
          导出按游戏引擎版本精确匹配 SDK（如游戏 8.3.2 优先用 8.3.2），无精确匹配时退回同大版本最新；
          缺失的 7.x/8.x 大版本会在启动时自动下载补齐。SDK 固定安装在数据目录的 tools/ 下。
        </n-text>
        <template v-if="sdkList.length">
          <n-space v-for="s in sdkList" :key="s.path" align="center">
            <n-tag size="small" type="success"
                   style="width: 66px; justify-content: center">{{ s.version }}</n-tag>
            <n-tag v-if="s.is_default" size="small" type="warning"
                   style="width: 48px; justify-content: center">默认</n-tag>
            <n-text style="font-size: 13px">{{ s.path }}</n-text>
          </n-space>
        </template>
        <n-text v-else depth="3" style="font-size: 13px">未安装任何 SDK</n-text>
        <n-space align="center">
          <n-button size="small" :render-icon="renderIcon(CloudDownloadOutline)"
                    @click="downloadSdk('8.5.3')">下载 8.5.3</n-button>
          <n-button size="small" :render-icon="renderIcon(CloudDownloadOutline)"
                    @click="downloadSdk('7.4.11')">下载 7.4.11</n-button>
          <n-text depth="3" style="font-size: 12px">其他版本：</n-text>
          <n-input v-model:value="sdkVersion" size="small" style="width: 90px"
                   placeholder="如 8.4.1" />
          <n-button size="small" :disabled="!sdkVersion.trim()"
                    @click="downloadSdk(sdkVersion.trim())">下载</n-button>
        </n-space>
      </n-space>
    </n-card>

    <n-card size="small" title="数据目录">
      <n-space vertical>
        <n-text depth="3" style="font-size: 12px">
          项目、配置、日志、导出等全部数据的位置。修改后会把现有数据迁移过去（迁移期间请勿操作）。
        </n-text>
        <n-text style="font-size: 13px">当前：{{ dataDir }}</n-text>
        <n-input-group style="width: 560px">
          <n-input v-model:value="newDataDir" placeholder="新数据目录" />
          <n-button v-if="guiMode" size="small"
                    :render-icon="renderIcon(FolderOpenOutline)"
                    @click="browseDataDir">浏览</n-button>
          <n-popconfirm @positive-click="migrateDataDir">
            <template #trigger>
              <n-button size="small" type="warning" :disabled="migrating">
                迁移并切换
              </n-button>
            </template>
            将把 {{ dataDir }} 的全部数据迁移到 {{ newDataDir }}，确定？
          </n-popconfirm>
        </n-input-group>
      </n-space>
    </n-card>

    <n-modal :show="migrating" preset="card" title="正在迁移数据目录"
             style="width: min(380px, 96vw)" :mask-closable="false" :closable="false">
      <n-space align="center">
        <n-spin size="small" />
        <n-text>正在迁移全部数据，请勿关闭应用...</n-text>
      </n-space>
    </n-modal>
  </div>
</template>
