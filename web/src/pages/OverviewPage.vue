<script setup lang="ts">
/** 项目概览：现在到哪了、下一步做什么、最近发生了什么 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  NButton, NCard, NEmpty, NProgress, NSpace, NTag, NText, useMessage,
} from 'naive-ui'
import PageHeader from '../components/PageHeader.vue'
import SetupChecklist from '../components/SetupChecklist.vue'
import UpdateReportDialog from '../components/UpdateReportDialog.vue'
import { RefreshOutline } from '@vicons/ionicons5'
import { renderIcon } from '../components/icons'
import { api, toastError, toastOk } from '../api/client'
import {
  NEXT_STEP_META, isChecklistDismissed, nextStep, projectChecklist,
  setChecklistDismissed,
} from '../composables/onboarding'
import { useJobsStore } from '../stores/jobs'
import { useSessionStore } from '../stores/session'
import { usePageScroll } from '../composables/usePageScroll'

const session = useSessionStore()
const jobsStore = useJobsStore()
const router = useRouter()
const message = useMessage()

// ---- 数据：导出信息（markup 计数）/ 项目包 / 最近任务 ----
const markupCount = ref(0)
const packageCount = ref(0)
const reportVisible = ref(false)

interface JobRow {
  id: string
  kind: string
  label: string
  status: string
  progress: number
  stage?: string
  created_at?: string
  error?: string | null
}
const recentJobs = ref<JobRow[]>([])

const sideLoading = ref(false)

async function loadSideData() {
  if (!session.currentProject) return
  try {
    const info = await api.get<{ markup_issues: { count: number } }>(
      '/api/current/export/info')
    markupCount.value = info.markup_issues.count
  } catch { /* 导出信息不可用不挡概览 */ }
  try {
    const pkgs = await api.get<unknown[]>(
      `/api/projects/${encodeURIComponent(session.currentProject)}/packages`)
    packageCount.value = pkgs.length
  } catch { /* 同上 */ }
  try {
    recentJobs.value = await api.get<JobRow[]>('/api/jobs?limit=5')
  } catch { /* 同上 */ }
}

onMounted(loadSideData)
usePageScroll(loadSideData)
// 切换项目后重载侧数据
watch(() => session.currentProject, loadSideData)

/** 手动刷新：会话统计 + 概览侧数据 */
async function reload() {
  sideLoading.value = true
  try {
    await session.refresh()
    await loadSideData()
  } finally {
    sideLoading.value = false
  }
}

// ---- 下一步与检查单 ----
const step = computed(() => nextStep(session, markupCount.value))
const stepMeta = computed(() => NEXT_STEP_META[step.value])
const checklistDismissed = ref(false)
watch(() => session.currentProject, (p) => {
  checklistDismissed.value = !!p && isChecklistDismissed(p)
}, { immediate: true })

const checklistItems = computed(() =>
  projectChecklist(session, packageCount.value > 0))
const checklistCurrent = computed(() => checklistItems.value.find((i) => !i.done)?.key)

function runStepAction() {
  if (step.value === 'done') {
    void revealPackages()
    return
  }
  const route = stepMeta.value.route
  if (route) void router.push(route)
}

function onChecklistAction(key: string) {
  const item = checklistItems.value.find((i) => i.key === key)
  if (!item) return
  if (key === 'export') {
    void router.push('/export')
    return
  }
  // model/project 路由固定，其余按 key 映射
  const routes: Record<string, string> = {
    model: '/settings?new=1', project: '/projects?dialog=create',
    names: '/names', trial: '/dialogue',
  }
  if (routes[key]) void router.push(routes[key])
}

function dismissChecklist() {
  if (!session.currentProject) return
  setChecklistDismissed(session.currentProject, true)
  checklistDismissed.value = true
  toastOk(message, '已收起新手引导（可从说明书找回）')
}

async function revealPackages() {
  if (!session.currentProject) return
  try {
    await api.post(`/api/projects/${encodeURIComponent(session.currentProject)}/packages/reveal`)
  } catch (e) { toastError(message, e) }
}

function openJob(row: JobRow) {
  jobsStore.track(row.id, row as never, true)
}

function jobAge(iso?: string) {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return ''
  const min = Math.floor((Date.now() - t) / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const h = Math.floor(min / 60)
  if (h < 24) return `${h} 小时前`
  return `${Math.floor(h / 24)} 天前`
}

// ---- 统计行 ----
const progressRows = computed(() => {
  const s = session.stats
  return [
    { label: '人名与角色', st: s.names },
    { label: 'UI 字符串', st: s.ui },
    { label: '对话', st: s.dialogue },
  ].filter((r) => !!r.st?.total)
})
</script>

<template>
  <div>
    <page-header :title="session.currentProject || '项目概览'">
      <template #actions>
        <n-button size="small" :loading="sideLoading" :render-icon="renderIcon(RefreshOutline)"
                  @click="reload">刷新</n-button>
      </template>
    </page-header>

    <n-empty v-if="!session.currentProject" description="未打开项目">
      <template #extra>
        <n-button type="primary" @click="router.push('/projects')">前往项目库</n-button>
      </template>
    </n-empty>

    <n-space v-else vertical size="medium">
      <!-- 下一步 -->
      <n-card size="small" :title="`下一步：${stepMeta.title}`">
        <n-space align="center" justify="space-between">
          <n-text depth="2" style="font-size: 13px">{{ stepMeta.desc }}</n-text>
          <n-button type="primary" size="small" @click="runStepAction">
            {{ stepMeta.cta }} →
          </n-button>
        </n-space>
      </n-card>

      <!-- 新手检查单（全部完成或手动收起后隐藏） -->
      <setup-checklist
        v-if="!checklistDismissed && checklistItems.some((i) => !i.done)"
        :items="checklistItems" :current-key="checklistCurrent"
        :current-cta="checklistCurrent === 'export' ? '去导出' : '去完成'"
        @action="onChecklistAction" @dismiss="dismissChecklist"
      />

      <!-- 进度 -->
      <n-card size="small" title="进度">
        <n-empty v-if="!progressRows.length" description="还没有可翻译的内容" size="small" />
        <n-space v-else vertical style="width: 100%">
          <div v-for="r in progressRows" :key="r.label"
               style="display: flex; align-items: center; gap: 12px">
            <n-text style="width: 90px">{{ r.label }}</n-text>
            <n-progress type="line" :percentage="Math.round((r.st!.translated / r.st!.total) * 100)"
                        style="flex: 1" />
            <n-text depth="3" style="font-size: 12px; width: 110px; text-align: right">
              {{ r.st!.translated }}/{{ r.st!.total }}
            </n-text>
          </div>
        </n-space>
      </n-card>

      <!-- 最近任务 -->
      <n-card size="small" title="最近任务">
        <n-empty v-if="!recentJobs.length" description="还没有任务记录" size="small" />
        <n-space v-else vertical style="width: 100%">
          <div v-for="j in recentJobs" :key="j.id"
               style="display: flex; align-items: center; gap: 10px">
            <n-tag size="tiny"
                   :type="j.status === 'failed' ? 'warning'
                     : j.status === 'running' ? 'info' : 'default'">
              {{ j.status === 'failed' ? '失败' : j.status === 'running' ? '进行中' : '完成' }}
            </n-tag>
            <n-text style="flex: 1; min-width: 0">{{ j.label || j.kind }}</n-text>
            <n-text depth="3" style="font-size: 12px">{{ jobAge(j.created_at) }}</n-text>
            <n-button size="tiny" quaternary @click="openJob(j)">详情</n-button>
          </div>
        </n-space>
      </n-card>

      <!-- 项目操作 -->
      <n-card size="small" title="项目操作">
        <n-space>
          <n-button size="small" @click="router.push('/export')">导出游戏</n-button>
          <n-button size="small" @click="router.push('/projects')">更新版本 / 管理</n-button>
          <n-button v-if="packageCount" size="small" @click="revealPackages">
            打开导出目录（{{ packageCount }} 个包）
          </n-button>
          <n-button size="small" @click="reportVisible = true">更新报告</n-button>
        </n-space>
      </n-card>
    </n-space>

    <update-report-dialog v-model:show="reportVisible"
                          :project-name="session.currentProject" />
  </div>
</template>
