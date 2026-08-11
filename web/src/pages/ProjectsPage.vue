<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import {
  NButton, NCard, NEmpty, NInput, NInputGroup, NModal, NPopconfirm,
  NRadioButton, NRadioGroup, NSelect, NSpace, NTag, NText, NUpload,
  useMessage,
} from 'naive-ui'
import type { UploadFileInfo } from 'naive-ui'
import { useSessionStore } from '../stores/session'
import { useProjectsStore, type ProjectItem } from '../stores/projects'
import { useJobsStore } from '../stores/jobs'
import { AddOutline, DownloadOutline, RefreshOutline } from '@vicons/ionicons5'
import { api, toastError, toastOk } from '../api/client'
import { renderIcon } from '../components/icons'
import { nativeReady, pickDirectory } from '../api/native'
import ProgressLine from '../components/ProgressLine.vue'
import UpdateReportDialog from '../components/UpdateReportDialog.vue'


const message = useMessage()
const session = useSessionStore()
const store = useProjectsStore()
const jobsStore = useJobsStore()
const guiMode = ref(false)

async function browseDir() {
  const dir = await pickDirectory()
  if (dir) createDir.value = dir
}

interface PackageInfo {
  file: string
  size: number
  mtime: string
}

const packages = ref<Record<string, PackageInfo[]>>({})
const modelOptions = ref<Array<{ label: string; value: string }>>([])

// ---- 新建项目 ----
const createVisible = ref(false)
const createName = ref('')
const createMethod = ref<'path' | 'zip'>('path')
const createDir = ref('')
const createZip = ref<File | null>(null)
const createModel = ref('')
const creating = ref(false)
const uploadPct = ref(0)   // zip 上传进度（-1 = 未开始）

function openCreate() {
  createName.value = ''
  createDir.value = ''
  createZip.value = null
  createMethod.value = 'path'
  creating.value = false
  uploadPct.value = -1
  createVisible.value = true
}

function onZipChange(options: { fileList: UploadFileInfo[] }) {
  const f = options.fileList[0]
  createZip.value = f?.file ?? null
}

async function submitCreate() {
  if (!createName.value.trim()) {
    message.warning('请填写项目名称')
    return
  }
  try {
    let jobId: string
    if (createMethod.value === 'path') {
      if (!createDir.value.trim()) {
        message.warning('请填写游戏目录')
        return
      }
      const data = await api.post<{ job_id: string }>('/api/projects/create', {
        name: createName.value, game_dir: createDir.value, model: createModel.value,
      })
      jobId = data.job_id
    } else {
      if (!createZip.value) {
        message.warning('请先选择 zip 文件')
        return
      }
      // 大 zip 上传是主要等待段：对话框内显示实时上传进度
      creating.value = true
      uploadPct.value = 0
      const form = new FormData()
      form.append('name', createName.value)
      form.append('model', createModel.value)
      form.append('file', createZip.value)
      const data = await api.postFormProgress<{ job_id: string }>(
        '/api/projects/create-zip', form,
        (pct) => { uploadPct.value = pct })
      jobId = data.job_id
    }
    createVisible.value = false
    jobsStore.track(jobId)
  } catch (e) {
    toastError(message, e)
  } finally {
    creating.value = false
  }
}

// ---- 导入项目 ----
const importVisible = ref(false)
const importName = ref('')
const importZip = ref<File | null>(null)
const importing = ref(false)
const importPct = ref(-1)

// ---- 更新版本 ----
const updateVisible = ref(false)
const updateName = ref('')
const updateMethod = ref<'path' | 'zip'>('path')
const updateDir = ref('')
const updateZip = ref<File | null>(null)
const updating = ref(false)
const updatePct = ref(-1)
const reportVisible = ref(false)
const reportName = ref('')

function openUpdate(p: ProjectItem) {
  updateName.value = p.name
  updateDir.value = ''
  updateZip.value = null
  updateMethod.value = 'path'
  updating.value = false
  updatePct.value = -1
  updateVisible.value = true
}

function openReport(name: string) {
  reportName.value = name
  reportVisible.value = true
}

async function browseUpdateDir() {
  const dir = await pickDirectory()
  if (dir) updateDir.value = dir
}

async function submitUpdate() {
  try {
    let jobId: string
    const encName = encodeURIComponent(updateName.value)
    if (updateMethod.value === 'path') {
      if (!updateDir.value.trim()) {
        message.warning('请填写新版本游戏目录')
        return
      }
      const data = await api.post<{ job_id: string }>(
        `/api/projects/${encName}/update`, { game_dir: updateDir.value })
      jobId = data.job_id
    } else {
      if (!updateZip.value) {
        message.warning('请先选择 zip 文件')
        return
      }
      updating.value = true
      updatePct.value = 0
      const form = new FormData()
      form.append('file', updateZip.value)
      const data = await api.postFormProgress<{ job_id: string }>(
        `/api/projects/${encName}/update-zip`, form,
        (pct) => { updatePct.value = pct })
      jobId = data.job_id
    }
    updateVisible.value = false
    jobsStore.track(jobId)
  } catch (e) {
    toastError(message, e)
  } finally {
    updating.value = false
  }
}

async function submitImport() {
  if (!importZip.value) {
    message.warning('请先选择 zip 文件')
    return
  }
  importing.value = true
  importPct.value = 0
  try {
    const form = new FormData()
    form.append('name', importName.value)
    form.append('file', importZip.value)
    const data = await api.postFormProgress<{ job_id: string }>(
      '/api/projects/import', form, (pct) => { importPct.value = pct })
    importVisible.value = false
    jobsStore.track(data.job_id)
  } catch (e) {
    toastError(message, e)
  } finally {
    importing.value = false
  }
}

// 项目类任务终结 → 刷新列表/会话/项目包；建项目成功 → 自动打开（对齐旧版）
// export.game 也会产生项目包（{项目名}-translated.zip），一并触发刷新
const handledJobs = new Set<string>()
watch(
  () => [...jobsStore.jobs.values()]
    .filter((j) => j.kind.startsWith('project.') || j.kind === 'export.game')
    .map((j) => `${j.id}:${j.status}`).join(','),
  async () => {
    await store.refresh()
    await session.refresh()
    for (const p of store.list) await loadPackages(p.name)
    for (const j of jobsStore.jobs.values()) {
      if (j.kind === 'project.create' && j.status === 'succeeded'
          && !handledJobs.has(j.id)) {
        handledJobs.add(j.id)
        const name = (j.payload?.name as string) || ''
        if (name) {
          try {
            await session.open(name)
            await store.refresh()
            toastOk(message, `项目 "${name}" 创建成功！`)
          } catch (e) {
            toastError(message, e)
          }
        }
      }
      // 版本更新成功 → 弹出更新报告（继承/新增/复核/失效统计）
      if (j.kind === 'project.update' && j.status === 'succeeded'
          && !handledJobs.has(j.id)) {
        handledJobs.add(j.id)
        const name = (j.payload?.name as string) || ''
        if (name) openReport(name)
      }
    }
  })

// ---- 编辑对话框 ----
const editVisible = ref(false)
const editOldName = ref('')
const editName = ref('')
const editModel = ref('')

async function openEdit(p: ProjectItem) {
  try {
    const meta = await api.get<{ name: string; model_config_name: string }>(
      `/api/projects/${encodeURIComponent(p.name)}/meta`)
    editOldName.value = p.name
    editName.value = meta.name
    editModel.value = meta.model_config_name
    editVisible.value = true
  } catch (e) {
    toastError(message, e)
  }
}

async function saveEdit() {
  try {
    await api.patch(`/api/projects/${encodeURIComponent(editOldName.value)}`, {
      new_name: editName.value,
      model: editModel.value,
    })
    editVisible.value = false
    toastOk(message, '项目已更新')
    await session.refresh()
    await store.refresh()
  } catch (e) {
    toastError(message, e)
  }
}

// ---- 操作 ----
async function openProject(p: ProjectItem) {
  try {
    await session.open(p.name)
    await store.refresh()
    toastOk(message, `已打开项目: ${p.name}`)
  } catch (e) {
    toastError(message, e)
  }
}

async function deleteProject(p: ProjectItem) {
  try {
    const data = await api.del<{ job_id: string }>(
      `/api/projects/${encodeURIComponent(p.name)}`)
    jobsStore.track(data.job_id)  // 进度/失败走任务对话框（含 traceback）
  } catch (e) {
    toastError(message, e)
  }
}

async function exportZip(p: ProjectItem) {
  try {
    const data = await api.post<{ job_id: string }>(
      `/api/projects/${encodeURIComponent(p.name)}/export-zip`)
    jobsStore.track(data.job_id)
  } catch (e) {
    toastError(message, e)
  }
}

async function loadPackages(name: string) {
  packages.value[name] = await api.get<PackageInfo[]>(
    `/api/projects/${encodeURIComponent(name)}/packages`)
}

async function revealPackages(name: string) {
  try {
    await api.post(`/api/projects/${encodeURIComponent(name)}/packages/reveal`)
  } catch (e) {
    toastError(message, e)
  }
}

onMounted(async () => {
  guiMode.value = await nativeReady()
  await store.refresh()
  const configs = await api.get<Array<{ name: string }>>('/api/configs')
  modelOptions.value = configs.map((c) => ({ label: c.name, value: c.name }))
  if (configs.length && !createModel.value) createModel.value = configs[0].name
  for (const p of store.list) await loadPackages(p.name)
})
</script>

<template>
  <div>
    <n-space align="center" style="margin-bottom: 12px">
      <h2 style="margin: 0">项目管理</h2>
      <n-button size="small" type="primary" :render-icon="renderIcon(AddOutline)" @click="openCreate">新建项目</n-button>
      <n-button size="small" :render-icon="renderIcon(DownloadOutline)" @click="importVisible = true">导入项目</n-button>
      <n-button size="small" quaternary :render-icon="renderIcon(RefreshOutline)" @click="store.refresh()">刷新</n-button>
    </n-space>

    <n-empty v-if="!store.list.length" description="暂无项目" style="margin-top: 60px" />

    <n-card v-for="p in store.list" :key="p.name" size="small" style="margin-bottom: 10px">
      <n-space align="center" justify="space-between">
        <n-space align="center">
          <n-text strong style="font-size: 15px">{{ p.name }}</n-text>
          <n-tag v-if="p.is_current" size="small" type="success">当前项目</n-tag>
          <n-tag size="small" :bordered="false">{{ p.model_config_name || '未配置模型' }}</n-tag>
        </n-space>
        <n-space>
          <n-button size="small" type="primary" :disabled="p.is_current" @click="openProject(p)">
            打开
          </n-button>
          <n-button size="small" @click="openEdit(p)">编辑</n-button>
          <n-button size="small" @click="openUpdate(p)">更新版本</n-button>
          <n-button size="small" @click="exportZip(p)">导出</n-button>
          <n-popconfirm @positive-click="deleteProject(p)">
            <template #trigger>
              <n-button size="small" type="error" quaternary>删除</n-button>
            </template>
            确定删除项目「{{ p.name }}」？此操作不可撤销。
          </n-popconfirm>
        </n-space>
      </n-space>

      <n-space align="center" style="margin-top: 8px">
        <progress-line
          :value="p.progress_percent / 100"
          style="width: 280px"
        />
        <n-text depth="3" style="font-size: 12px">{{ p.progress_text }}</n-text>
      </n-space>

      <div style="margin-top: 8px">
        <n-button v-if="packages[p.name]?.length" size="small" quaternary @click="revealPackages(p.name)">
          打开导出目录（{{ packages[p.name].length }} 个包）
        </n-button>
        <n-button size="small" quaternary @click="openReport(p.name)">更新报告</n-button>
      </div>
    </n-card>

    <n-modal v-model:show="editVisible" preset="card" title="编辑项目" style="width: 420px">
      <n-space vertical>
        <n-input v-model:value="editName" placeholder="项目名称" />
        <n-select v-model:value="editModel" :options="modelOptions" placeholder="AI模型" clearable />
      </n-space>
      <template #footer>
        <n-space justify="end">
          <n-button @click="editVisible = false">取消</n-button>
          <n-button type="primary" @click="saveEdit">保存</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- 新建项目 -->
    <n-modal v-model:show="createVisible" preset="card" title="新建项目" style="width: 520px">
      <n-space vertical>
        <n-input v-model:value="createName" placeholder="项目名称" />
        <n-radio-group v-model:value="createMethod" size="small">
          <n-radio-button value="path">路径输入</n-radio-button>
          <n-radio-button value="zip">上传 zip</n-radio-button>
        </n-radio-group>
        <n-input-group v-if="createMethod === 'path'">
          <n-input
            v-model:value="createDir"
            placeholder="游戏目录（含 game/ 的上一级或游戏根目录）"
          />
          <n-button v-if="guiMode" size="small" @click="browseDir">浏览…</n-button>
        </n-input-group>
        <n-upload
          v-else
          :max="1" accept=".zip"
          :default-upload="false"
          @change="onZipChange"
        >
          <n-button size="small">选择游戏 zip</n-button>
          <span v-if="createZip" style="margin-left: 8px; font-size: 12px">{{ createZip.name }}</span>
        </n-upload>
        <n-select v-model:value="createModel" :options="modelOptions" placeholder="AI模型" />
        <div v-if="creating && uploadPct >= 0">
          <n-text depth="3" style="font-size: 12px">正在上传 zip（本地传输，大文件需等待）...</n-text>
          <progress-line :value="uploadPct" processing />
        </div>
      </n-space>
      <template #footer>
        <n-space justify="end">
          <n-button :disabled="creating" @click="createVisible = false">取消</n-button>
          <n-button type="primary" :loading="creating" @click="submitCreate">创建</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- 更新版本 -->
    <n-modal v-model:show="updateVisible" preset="card" :title="`更新版本 — ${updateName}`" style="width: 520px">
      <n-space vertical>
        <n-text depth="3" style="font-size: 12px">
          选择新版本游戏（目录或 zip）。现有译文、人名、术语、风格指南都会保留，
          已翻译文本按原文自动继承，只有新增/改动的文本需要翻译。更新前会自动备份，可回滚。
        </n-text>
        <n-radio-group v-model:value="updateMethod" size="small">
          <n-radio-button value="path">路径输入</n-radio-button>
          <n-radio-button value="zip">上传 zip</n-radio-button>
        </n-radio-group>
        <n-input-group v-if="updateMethod === 'path'">
          <n-input
            v-model:value="updateDir"
            placeholder="新版本游戏目录（含 game/ 的上一级或游戏根目录）"
          />
          <n-button v-if="guiMode" size="small" @click="browseUpdateDir">浏览…</n-button>
        </n-input-group>
        <n-upload
          v-else
          :max="1" accept=".zip"
          :default-upload="false"
          @change="(o: { fileList: UploadFileInfo[] }) => updateZip = o.fileList[0]?.file ?? null"
        >
          <n-button size="small">选择新版本 zip</n-button>
          <span v-if="updateZip" style="margin-left: 8px; font-size: 12px">{{ updateZip.name }}</span>
        </n-upload>
        <div v-if="updating && updatePct >= 0">
          <n-text depth="3" style="font-size: 12px">正在上传 zip（本地传输，大文件需等待）...</n-text>
          <progress-line :value="updatePct" processing />
        </div>
      </n-space>
      <template #footer>
        <n-space justify="end">
          <n-button :disabled="updating" @click="updateVisible = false">取消</n-button>
          <n-button type="primary" :loading="updating" @click="submitUpdate">开始更新</n-button>
        </n-space>
      </template>
    </n-modal>

    <update-report-dialog v-model:show="reportVisible" :project-name="reportName" />

    <!-- 导入项目 -->
    <n-modal v-model:show="importVisible" preset="card" title="导入项目" style="width: 480px">
      <n-space vertical>
        <n-input v-model:value="importName" placeholder="项目名称（留空则用包内名称）" />
        <n-upload
          :max="1" accept=".zip" :default-upload="false"
          @change="(o: { fileList: UploadFileInfo[] }) => importZip = o.fileList[0]?.file ?? null"
        >
          <n-button size="small">选择项目包 zip</n-button>
          <span v-if="importZip" style="margin-left: 8px; font-size: 12px">{{ importZip.name }}</span>
        </n-upload>
        <div v-if="importing && importPct >= 0">
          <n-text depth="3" style="font-size: 12px">正在上传项目包...</n-text>
          <progress-line :value="importPct" processing />
        </div>
      </n-space>
      <template #footer>
        <n-space justify="end">
          <n-button :disabled="importing" @click="importVisible = false">取消</n-button>
          <n-button type="primary" :loading="importing" @click="submitImport">导入</n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>
