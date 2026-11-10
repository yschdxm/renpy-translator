<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NButton, NCard, NEmpty, NInput, NInputGroup, NModal, NPopconfirm,
  NRadioButton, NRadioGroup, NSpace, NTag, NText,
  useMessage,
} from 'naive-ui'
import { useSessionStore } from '../stores/session'
import { useProjectsStore, type ProjectItem } from '../stores/projects'
import { useJobsStore } from '../stores/jobs'
import { useUploadsStore } from '../stores/uploads'
import { AddOutline, DownloadOutline, RefreshOutline } from '@vicons/ionicons5'
import { api, isUploadCancelled, toastError, toastOk } from '../api/client'
import { renderIcon } from '../components/icons'
import { nativeReady, pickDirectory } from '../api/native'
import FileDropSelect from '../components/FileDropSelect.vue'
import ProgressLine from '../components/ProgressLine.vue'
import PageHelp from '../components/PageHelp.vue'
import SetupChecklist from '../components/SetupChecklist.vue'
import UpdateReportDialog from '../components/UpdateReportDialog.vue'
import { usePageScroll } from '../composables/usePageScroll'


const message = useMessage()
const route = useRoute()
const router = useRouter()
const session = useSessionStore()
const store = useProjectsStore()
const jobsStore = useJobsStore()
const uploadsStore = useUploadsStore()
const guiMode = ref(false)

/** 各表单对话框当前对应的上传任务（内联进度 + 取消） */
const createUpload = computed(() => uploadsStore.ofKind('create'))
const importUpload = computed(() => uploadsStore.ofKind('import'))
const updateUpload = computed(() => uploadsStore.ofKind('update'))

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

// ---- 新建项目 ----
const createVisible = ref(false)
const createName = ref('')
const createMethod = ref<'path' | 'zip'>('path')
const createDir = ref('')
const createZip = ref<File | null>(null)

function openCreate() {
  createName.value = ''
  createDir.value = ''
  createZip.value = null
  createMethod.value = 'path'
  createVisible.value = true
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
        name: createName.value, game_dir: createDir.value,
      })
      jobId = data.job_id
    } else {
      if (!createZip.value) {
        message.warning('请先选择 zip 文件')
        return
      }
      // 大 zip 上传是主要等待段：任务化——对话框内联进度，关闭对话框
      // 不中断（转顶栏后台任务列表），可随时取消
      const form = new FormData()
      form.append('name', createName.value)
      form.append('file', createZip.value)
      const data = await uploadsStore.upload<{ job_id: string }>(
        'create', `上传 ${createZip.value.name}（新建项目）`,
        '/api/projects/create-zip', form)
      jobId = data.job_id
    }
    createVisible.value = false
    jobsStore.track(jobId)
  } catch (e) {
    if (isUploadCancelled(e)) return // 用户取消，无需报错
    toastError(message, e)
  }
}

// ---- 导入项目 ----
const importVisible = ref(false)
const importName = ref('')
const importZip = ref<File | null>(null)

// ---- 更新版本 ----
const updateVisible = ref(false)
const updateName = ref('')
const updateMethod = ref<'path' | 'zip'>('path')
const updateDir = ref('')
const updateZip = ref<File | null>(null)
const reportVisible = ref(false)
const reportName = ref('')

// 对话框开合 ⇄ 上传任务可见性：开着内联展示，关了转顶栏「后台任务」列表
watch(createVisible, (v) => uploadsStore.setDialogOpen('create', v))
watch(updateVisible, (v) => uploadsStore.setDialogOpen('update', v))
watch(importVisible, (v) => uploadsStore.setDialogOpen('import', v))

function openUpdate(p: ProjectItem) {
  updateName.value = p.name
  updateDir.value = ''
  updateZip.value = null
  updateMethod.value = 'path'
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
      const form = new FormData()
      form.append('file', updateZip.value)
      const data = await uploadsStore.upload<{ job_id: string }>(
        'update', `上传 ${updateZip.value.name}（更新版本）`,
        `/api/projects/${encName}/update-zip`, form)
      jobId = data.job_id
    }
    updateVisible.value = false
    jobsStore.track(jobId)
  } catch (e) {
    if (isUploadCancelled(e)) return // 用户取消，无需报错
    toastError(message, e)
  }
}

async function submitImport() {
  if (!importZip.value) {
    message.warning('请先选择 zip 文件')
    return
  }
  try {
    const form = new FormData()
    form.append('name', importName.value)
    form.append('file', importZip.value)
    const data = await uploadsStore.upload<{ job_id: string }>(
      'import', `上传 ${importZip.value.name}（导入项目）`,
      '/api/projects/import', form)
    importVisible.value = false
    jobsStore.track(data.job_id)
  } catch (e) {
    if (isUploadCancelled(e)) return // 用户取消，无需报错
    toastError(message, e)
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
            // 建项即进入下一步：落到概览（检查单/下一步卡）
            void router.push('/overview')
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

// ---- 重命名对话框 ----
const editVisible = ref(false)
const editOldName = ref('')
const editName = ref('')

async function openEdit(p: ProjectItem) {
  try {
    const meta = await api.get<{ name: string }>(
      `/api/projects/${encodeURIComponent(p.name)}/meta`)
    editOldName.value = p.name
    editName.value = meta.name
    editVisible.value = true
  } catch (e) {
    toastError(message, e)
  }
}

async function saveEdit() {
  try {
    await api.patch(`/api/projects/${encodeURIComponent(editOldName.value)}`, {
      new_name: editName.value,
    })
    editVisible.value = false
    toastOk(message, '项目已重命名')
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
  for (const p of store.list) await loadPackages(p.name)
})
usePageScroll(() => store.refresh())

// 顶栏项目菜单的「新建/导入…」经 ?dialog=create|import 触发本页弹窗
watch(() => route.query.dialog, (v) => {
  if (v === 'create') openCreate()
  else if (v === 'import') importVisible.value = true
  if (v) void router.replace({ query: {} })
}, { immediate: true })

// ---- 首启空态（两步引导，可收起） ----
const firstRunDismissed = ref(
  (() => { try { return localStorage.getItem('rt.onboarding.firstRun.dismissed') === '1' } catch { return false } })())
const firstRunItems = computed(() => [
  {
    key: 'model', title: '配置 AI 模型',
    desc: 'DeepSeek / GPT / Claude 等，填 API Key 即可（约 2 分钟）',
    done: session.hasTranslator,
  },
  {
    key: 'project', title: '新建项目',
    desc: '选择游戏文件夹或上传 zip，自动解包、反编译；已有项目包可「导入项目」',
    done: false,
  },
])
const firstRunStep = computed(() => (session.hasTranslator ? 'project' : 'model'))
const firstRunCta = computed(() => (firstRunStep.value === 'model' ? '配置模型' : '新建项目'))

function onFirstRunAction(key: string) {
  if (key === 'model') void router.push('/settings?new=1')
  else openCreate()
}

function dismissFirstRun() {
  firstRunDismissed.value = true
  try { localStorage.setItem('rt.onboarding.firstRun.dismissed', '1') } catch { /* 忽略 */ }
}
</script>

<template>
  <div>
    <n-space align="center" style="width: 100%; margin-bottom: 12px">
      <h2 style="margin: 0">项目管理</h2>
      <page-help style="margin-left: auto" :section="'3,12'" />
      <n-button size="small" type="primary" :render-icon="renderIcon(AddOutline)" @click="openCreate">新建项目</n-button>
      <n-button size="small" :render-icon="renderIcon(DownloadOutline)" @click="importVisible = true">导入项目</n-button>
      <n-button size="small" quaternary :render-icon="renderIcon(RefreshOutline)" @click="store.refresh()">刷新</n-button>
    </n-space>

    <div v-if="!store.list.length" style="margin-top: 40px; max-width: 560px">
      <n-empty v-if="firstRunDismissed" description="暂无项目" />
      <setup-checklist
        v-else :items="firstRunItems" :current-key="firstRunStep"
        :current-cta="firstRunCta" @action="onFirstRunAction"
        @dismiss="dismissFirstRun"
      />
    </div>

    <n-card v-for="p in store.list" :key="p.name" size="small" style="margin-bottom: 10px">
      <n-space align="center" justify="space-between">
        <n-space align="center">
          <n-text strong style="font-size: 15px">{{ p.name }}</n-text>
          <n-tag v-if="p.is_current" size="small" type="success">当前项目</n-tag>
        </n-space>
        <n-space>
          <n-button size="small" type="primary" :disabled="p.is_current" @click="openProject(p)">
            打开
          </n-button>
          <n-button size="small" @click="openEdit(p)">重命名</n-button>
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

    <n-modal v-model:show="editVisible" preset="card" title="重命名项目" style="width: min(420px, 96vw)">
      <n-input v-model:value="editName" placeholder="项目名称" />
      <template #footer>
        <n-space justify="end">
          <n-button @click="editVisible = false">取消</n-button>
          <n-button type="primary" @click="saveEdit">保存</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- 新建项目 -->
    <n-modal v-model:show="createVisible" preset="card" title="新建项目" style="width: min(520px, 96vw)">
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
        <file-drop-select
          v-else v-model="createZip" accept=".zip"
          placeholder="点击或拖拽选择游戏 zip"
        />
        <div v-if="createUpload">
          <n-space align="center" justify="space-between">
            <n-text depth="3" style="font-size: 12px">
              正在上传 zip（本地传输，大文件需等待）——关闭对话框不会中断
            </n-text>
            <n-button size="tiny" type="error" quaternary
                      @click="uploadsStore.cancel(createUpload.id)">取消上传</n-button>
          </n-space>
          <progress-line :value="createUpload.pct" processing />
        </div>
      </n-space>
      <template #footer>
        <n-space justify="end">
          <n-button @click="createVisible = false">
            {{ createUpload ? '关闭（后台继续）' : '取消' }}
          </n-button>
          <n-button type="primary" :loading="!!createUpload" :disabled="!!createUpload"
                    @click="submitCreate">创建</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- 更新版本 -->
    <n-modal v-model:show="updateVisible" preset="card" :title="`更新版本 — ${updateName}`" style="width: min(520px, 96vw)">
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
        <file-drop-select
          v-else v-model="updateZip" accept=".zip"
          placeholder="点击或拖拽选择新版本 zip"
        />
        <div v-if="updateUpload">
          <n-space align="center" justify="space-between">
            <n-text depth="3" style="font-size: 12px">
              正在上传 zip（本地传输，大文件需等待）——关闭对话框不会中断
            </n-text>
            <n-button size="tiny" type="error" quaternary
                      @click="uploadsStore.cancel(updateUpload.id)">取消上传</n-button>
          </n-space>
          <progress-line :value="updateUpload.pct" processing />
        </div>
      </n-space>
      <template #footer>
        <n-space justify="end">
          <n-button @click="updateVisible = false">
            {{ updateUpload ? '关闭（后台继续）' : '取消' }}
          </n-button>
          <n-button type="primary" :loading="!!updateUpload" :disabled="!!updateUpload"
                    @click="submitUpdate">开始更新</n-button>
        </n-space>
      </template>
    </n-modal>

    <update-report-dialog v-model:show="reportVisible" :project-name="reportName" />

    <!-- 导入项目 -->
    <n-modal v-model:show="importVisible" preset="card" title="导入项目" style="width: min(480px, 96vw)">
      <n-space vertical>
        <n-input v-model:value="importName" placeholder="项目名称（留空则用包内名称）" />
        <file-drop-select
          v-model="importZip" accept=".zip"
          placeholder="点击或拖拽选择项目包 zip"
        />
        <div v-if="importUpload">
          <n-space align="center" justify="space-between">
            <n-text depth="3" style="font-size: 12px">
              正在上传项目包——关闭对话框不会中断
            </n-text>
            <n-button size="tiny" type="error" quaternary
                      @click="uploadsStore.cancel(importUpload.id)">取消上传</n-button>
          </n-space>
          <progress-line :value="importUpload.pct" processing />
        </div>
      </n-space>
      <template #footer>
        <n-space justify="end">
          <n-button @click="importVisible = false">
            {{ importUpload ? '关闭（后台继续）' : '取消' }}
          </n-button>
          <n-button type="primary" :loading="!!importUpload" :disabled="!!importUpload"
                    @click="submitImport">导入</n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>
