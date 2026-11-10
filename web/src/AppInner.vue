<script setup lang="ts">
/** 应用主体（在 message/dialog provider 内，可用 useMessage） */
import { computed, h, onMounted, onUnmounted, provide, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NBadge, NLayout, NLayoutHeader, NLayoutSider, NLayoutContent, NMenu, NModal,
  NSwitch, NTag, NButton, NDropdown, NEllipsis, NSpace, NText, useMessage,
} from 'naive-ui'
import type { DropdownOption, MenuOption } from 'naive-ui'
import { useSessionStore } from './stores/session'
import { useProjectsStore } from './stores/projects'
import { useJobsStore, type JobView } from './stores/jobs'
import { useUploadsStore } from './stores/uploads'
import JobProgressDialog from './components/JobProgressDialog.vue'
import JobsDrawer from './components/JobsDrawer.vue'
import UploadProgressDialog from './components/UploadProgressDialog.vue'
import EmbeddedReviewDialog from './components/EmbeddedReviewDialog.vue'
import { api, toastError } from './api/client'
import { openUrl } from './api/native'
import { SCROLL_EL_KEY, clearPageScrollPositions, recordScrollPosition } from './composables/usePageScroll'

const message = useMessage()
const session = useSessionStore()
const projectsStore = useProjectsStore()
const jobsStore = useJobsStore()
const uploadsStore = useUploadsStore()
const route = useRoute()
const router = useRouter()

// ---- 页面状态保留（keep-alive 全页面） ----
// key = path + 项目 + 项目epoch：/strings 与 /dialogue 各自缓存实例；
// 切项目重挂载且 epoch 递增（A→B→A 不取回 A 的旧缓存）
const projectEpoch = ref(0)
watch(() => session.currentProject, () => {
  projectEpoch.value++
  clearPageScrollPositions()
})
const pageInstanceKey = computed(
  () => `${route.path}|${session.currentProject}|${projectEpoch.value}`)

// 滚动容器（共享 n-layout-content）：provide 给各页 usePageScroll 按路由存取
const contentRef = ref()
function getScrollEl(): HTMLElement | null {
  const root = contentRef.value?.$el as HTMLElement | undefined
  if (!root) return null
  // 实际滚动元素是 .n-layout-scroll-container（overflow:auto）；
  // 必须先于 .n-scrollbar-container——后者会命中表格等组件内部的
  // 非滚动容器（scrollTop 恒 0，存了也白存）
  return (root.querySelector(':scope > .n-layout-scroll-container') as HTMLElement)
    || (root.querySelector('.n-layout-scroll-container') as HTMLElement)
    || (root.querySelector('.n-scrollbar-container') as HTMLElement)
    || root
}
provide(SCROLL_EL_KEY, getScrollEl)

// 路由离开前（DOM 未变）保存滚动位置——onDeactivated 时新页面已挂载，
// 共享容器 scrollTop 可能已被钳 0
router.beforeEach((to, from) => {
  const el = getScrollEl()
  if (el && from.path !== to.path) recordScrollPosition(from.path, el.scrollTop)
  return true
})

/** 侧栏三段式分组：流程 / 分析 / 设置；无项目时流程组退化为单个「项目库」入口 */
const menuOptions = computed<MenuOption[]>(() => {
  const hasProject = !!session.currentProject
  const flowChildren: MenuOption[] = hasProject
    ? [
        { label: '概览', key: '/overview' },
        { label: '人名与角色', key: '/names' },
        { label: 'UI 字符串', key: '/strings' },
        { label: '对话', key: '/dialogue' },
        { label: '术语表', key: '/glossary' },
        { label: '导出游戏', key: '/export' },
        { label: '定时任务', key: '/schedule' },
      ]
    : [{ label: '项目库', key: '/projects' }]
  const groups: MenuOption[] = [
    { type: 'group', key: 'g-flow', label: '流程', children: flowChildren },
  ]
  if (hasProject) {
    groups.push({
      type: 'group', key: 'g-analysis', label: '分析', children: [
        { label: '剧情分支图', key: '/story-graph', extra: '实验' },
        { label: '人物关系图谱', key: '/relations', extra: '实验' },
      ],
    })
  }
  groups.push({
    type: 'group', key: 'g-settings', label: '设置', children: [
      {
        label: '模型配置', key: '/settings',
        // 未配置 = 红点提示（可点击性由主操作禁用+本页入口承担）
        extra: session.hasTranslator ? undefined
          : () => h(NTag, { size: 'tiny', type: 'warning' }, () => '未配置'),
      },
      { label: '路径配置', key: '/paths' },
      { label: 'MCP 接入', key: '/mcp-access' },
      { label: '提示词设置', key: '/prompts' },
      { label: '诊断', key: '/diagnostics' },
    ],
  })
  return groups
})

const activeKey = computed(() => route.path)

// ---- 顶栏帮助/关于菜单（检查更新等低频维护收进这里） ----
const aboutVisible = ref(false)
const appVersion = ref('')
const helpMenuOptions: DropdownOption[] = [
  { label: '检查更新', key: 'check-update' },
  { label: '关于', key: 'about' },
]

function onHelpMenuSelect(key: string) {
  if (key === 'check-update') checkUpdate(false)
  else if (key === 'about') aboutVisible.value = true
}

// ---- 顶栏项目上下文菜单（切换/新建/导入/管理/关闭） ----
/** 项目下拉只管「切换项目」；新建/导入/管理/关闭是旁边看得见的按钮
 *  （全部藏进菜单会把管理入口埋太深） */
const switchOptions = computed<DropdownOption[]>(() =>
  projectsStore.list.map((p) => ({
    label: p.name === session.currentProject ? `${p.name}（当前）` : p.name,
    key: 'open:' + p.name,
    disabled: p.name === session.currentProject,
  })))

async function onProjectMenuSelect(key: string) {
  if (key.startsWith('open:')) {
    await onProjectChange(key.slice(5))
  }
}

/** 新建/导入：跳项目库并用 query 触发对应弹窗 */
async function gotoProjectsDialog(dialog: 'create' | 'import') {
  if (route.path !== '/projects') {
    await router.push({ path: '/projects', query: { dialog } })
  } else {
    await router.replace({ path: '/projects', query: { dialog } })
  }
}

async function closeProject() {
  try {
    await session.close()
    await projectsStore.refresh()
    await router.push('/projects')
  } catch (e) {
    toastError(message, e)
  }
}

/** 打开对话框的任务（含刷新后恢复的） */
const dialogJobs = computed(() =>
  [...jobsStore.jobs.values()].filter((j) => j.dialogOpen))

// ---- 任务中心（状态栏 + 铃铛 + 抽屉） ----
const jobsDrawerOpen = ref(false)

/** 状态栏「现在正在发生什么」：等待确认 > 运行摘要 > 就绪 */
const waitingJobs = computed(() => jobsStore.waitingJobs)
const failedInView = computed(() => jobsStore.failedInView)
const activeCount = computed(() =>
  jobsStore.activeJobs.length + uploadsStore.backgroundTasks.length)

const runningSummary = computed(() => {
  const j = jobsStore.activeJobs.find((x) => x.status === 'running')
  if (!j) return ''
  return `${j.label || '任务'} ${Math.round(j.progress * 100)}%${j.stage ? ' · ' + j.stage : ''}`
})

/** 铃铛徽标：等待确认/本会话失败（红）；否则进行中任务数（蓝）——运行中也要可见 */
const bellBadge = computed(() => {
  if (waitingJobs.value.length) return waitingJobs.value.length
  if (failedInView.value.length) return failedInView.value.length
  return activeCount.value
})
const bellType = computed<'error' | 'info'>(() =>
  (waitingJobs.value.length || failedInView.value.length) ? 'error' : 'info')

function openJobsDrawer() {
  jobsDrawerOpen.value = true
}

function openFirstWaiting() {
  const j = waitingJobs.value[0]
  if (j) jobsStore.openDialog(j.id)
  else jobsDrawerOpen.value = true
}

function onOpenJobsEvent() {
  jobsDrawerOpen.value = true
}

function onDrawerKeydown(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'j') {
    e.preventDefault()
    jobsDrawerOpen.value = !jobsDrawerOpen.value
  }
}

async function onProjectChange(name: string | null) {
  if (!name) return
  try {
    await session.open(name)
    await projectsStore.refresh()
  } catch (e) {
    // 打开失败（项目被删/db 损坏）：提示并刷新会话与列表，
    // 选择器绑定 session.currentProject，刷新后自动回退到实际当前项目
    toastError(message, e)
    await session.refresh()
    await projectsStore.refresh()
  }
}

function onMenu(key: string) {
  router.push(key)
}

/** 通用 confirm 问题回答：action 如 'delete'/'keep'/'cancel'（也兼容布尔） */
async function answerConfirm(job: JobView, action: string | boolean) {
  const q = job.question
  if (!q) return
  try {
    await jobsStore.answer(job.id, q.question_id,
      typeof action === 'boolean' ? { ok: action } : { action })
  } catch (e) {
    toastError(message, e)
  }
}

// ---- 检查更新（GitHub Release） ----

interface UpdateInfo {
  current: string
  latest: string
  has_update: boolean
  url: string
  notes: string
}

const updateInfo = ref<UpdateInfo | null>(null)
const updateVisible = ref(false)
const checkingUpdate = ref(false)
const autoUpdateCheck = ref(true)

/** silent=true：自动检查，失败/无更新不提示；手动检查给出反馈 */
async function checkUpdate(silent: boolean) {
  if (checkingUpdate.value) return
  checkingUpdate.value = true
  try {
    const info = await api.get<UpdateInfo>('/api/updates/check')
    if (info.has_update) {
      updateInfo.value = info
      updateVisible.value = true
    } else if (!silent) {
      message.success(`已是最新版本（v${info.current}）`)
    }
  } catch (e) {
    if (!silent) toastError(message, e)
  } finally {
    checkingUpdate.value = false
  }
}

async function onAutoCheckChange(v: boolean) {
  try {
    await api.put('/api/updates/auto-check', { enabled: v })
    autoUpdateCheck.value = v
  } catch (e) {
    toastError(message, e)  // 保存失败保持原状态
  }
}

async function gotoDownload() {
  if (updateInfo.value?.url) await openUrl(updateInfo.value.url)
  updateVisible.value = false
}

// ---- 服务心跳：连续失败显示「服务已停止」遮罩（浏览器无法自动关页） ----
const serverDown = ref(false)
// 迁移后自动重启中：显示「正在重启」遮罩，服务恢复后整页刷新
const restarting = ref(false)
let heartbeatTimer: number | undefined
let heartbeatFails = 0
let appEvents: EventSource | undefined

async function heartbeat() {
  if (restarting.value) return  // 重启期间由 pollRestart 负责探测
  try {
    const resp = await fetch('/api/health', {
      signal: AbortSignal.timeout(5000),
    })
    if (!resp.ok) throw new Error(String(resp.status))
    heartbeatFails = 0
    serverDown.value = false
    // 顺带取版本号（关于菜单展示）
    try {
      const data = await resp.json()
      if (typeof data.version === 'string') appVersion.value = data.version
    } catch { /* 版本号拿不到不影响心跳 */ }
  } catch {
    heartbeatFails++
    if (heartbeatFails >= 2) serverDown.value = true
  }
}

/** 自动重启期间轮询健康检查，恢复后整页刷新（数据根已换，前端状态全重来） */
async function pollRestart() {
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 2000))
    try {
      const resp = await fetch('/api/health', {
        signal: AbortSignal.timeout(3000),
      })
      if (resp.ok) {
        location.reload()
        return
      }
    } catch { /* 服务还没起来 */ }
  }
  // 超时：按服务停止处理，让用户手动重连
  restarting.value = false
  serverDown.value = true
}

/** 后端优雅退出时广播 shutdown：GUI 自行关窗；浏览器显示遮罩 */
function onAppEvent(ev: MessageEvent) {
  const data = JSON.parse(ev.data)
  if (data.type === 'shutdown') {
    const pvw = (window as { pywebview?: { api: { close_window(): void } } }).pywebview
    if (pvw) {
      pvw.api.close_window()
    } else {
      serverDown.value = true
    }
  } else if (data.type === 'restarting') {
    restarting.value = true
    pollRestart()
  }
}

onMounted(async () => {
  // 应用级事件流（服务关停广播）
  appEvents = new EventSource('/api/events')
  appEvents.addEventListener('app', onAppEvent)
  // 看门狗提示（服务异常死亡）：仅提示，不关窗
  window.addEventListener('rt-server-lost', () => { serverDown.value = true })

  // 任务中心入口：toast「查看任务」与 Ctrl+J
  window.addEventListener('keydown', onDrawerKeydown)
  window.addEventListener('rt:open-jobs', onOpenJobsEvent)

  heartbeatTimer = setInterval(heartbeat, 5000)
  await session.refresh()
  await projectsStore.refresh()
  await jobsStore.restore()
  // 自动检查更新：读取开关（默认开），开着则检查——失败/无更新静默
  try {
    const { enabled } = await api.get<{ enabled: boolean }>('/api/updates/auto-check')
    autoUpdateCheck.value = enabled
    if (enabled) checkUpdate(true)
  } catch { /* 设置读取失败按默认（开）处理，不再触发检查 */ }
  // 中断任务只给一条汇总提示（不逐条弹窗；翻译类重发即自动跳过已完成部分）
  if (session.interruptedJobs > 0) {
    message.warning(
      `上次服务退出时有 ${session.interruptedJobs} 个任务未跑完，已标记为中断。` +
      '重新发起即可（已翻译的部分会自动跳过）',
      { duration: 8000 })
  }
})

onUnmounted(() => {
  clearInterval(heartbeatTimer)
  appEvents?.close()
  window.removeEventListener('keydown', onDrawerKeydown)
  window.removeEventListener('rt:open-jobs', onOpenJobsEvent)
})
</script>

<template>
  <n-layout style="height: 100vh">
    <n-layout-header bordered style="padding: 0 16px; height: 52px; display: flex; align-items: center; gap: 8px; overflow: hidden">
      <span style="font-size: 16px; font-weight: 600; white-space: nowrap">Ren'Py 翻译工具</span>
      <!-- 切换项目：实边按钮 + 常显 ▾，一眼可点 -->
      <n-dropdown trigger="click" :options="switchOptions" @select="onProjectMenuSelect">
        <n-button size="small" style="max-width: 240px; flex: none">
          <n-ellipsis style="max-width: 190px">
            {{ session.currentProject || '选择项目' }}
          </n-ellipsis>
          <span style="margin-left: 6px; opacity: 0.75">▾</span>
        </n-button>
      </n-dropdown>
      <!-- 进度：完整展示不截断 -->
      <n-tag v-if="session.progressText" size="small" type="success" style="flex: none">
        {{ session.progressText }}
      </n-tag>
      <n-tag v-if="session.currentProject && !session.hasTranslator" size="small" type="warning"
             style="cursor: pointer; flex: none" @click="router.push('/settings?new=1')">
        未配置模型 · 去配置
      </n-tag>
      <!-- 项目操作：实体按钮并排，不埋进菜单 -->
      <n-button size="small" quaternary style="flex: none"
                title="新建项目" @click="gotoProjectsDialog('create')">新建</n-button>
      <n-button size="small" quaternary style="flex: none"
                title="导入项目包" @click="gotoProjectsDialog('import')">导入</n-button>
      <n-button size="small" quaternary style="flex: none"
                title="管理项目（项目库）" @click="router.push('/projects')">管理</n-button>
      <n-button v-if="session.currentProject" size="small" quaternary style="flex: none"
                title="关闭当前项目" @click="closeProject">关闭</n-button>
      <span style="flex: 1" />
      <!-- 铃铛：入口常驻（才能被学会），徽标=等待确认/失败数 -->
      <n-badge :value="bellBadge" :max="99" :show="bellBadge > 0" :type="bellType">
        <n-button size="tiny" quaternary @click="openJobsDrawer">任务中心</n-button>
      </n-badge>
      <!-- 帮助/关于：低频维护不再占黄金位 -->
      <n-dropdown trigger="click" placement="bottom-end"
                  :options="helpMenuOptions" @select="onHelpMenuSelect">
        <n-button size="tiny" quaternary>?</n-button>
      </n-dropdown>
    </n-layout-header>
    <n-layout has-sider position="absolute" style="top: 52px">
      <n-layout-sider bordered :width="180">
        <n-menu
          :value="activeKey"
          :options="menuOptions"
          @update:value="onMenu"
        />
      </n-layout-sider>
      <n-layout-content ref="contentRef" content-style="padding: 16px; padding-bottom: 40px">
        <!-- keep-alive 全页面：切走保留页码/筛选/滚动位置，返回时按当前查询
             后台刷新（usePageScroll）；key=path+项目+epoch 保证 /strings 与
             /dialogue 各自实例、切项目重挂载；max 限制缓存内存增长 -->
        <router-view v-slot="{ Component }">
          <keep-alive :max="12">
            <component :is="Component" :key="pageInstanceKey" />
          </keep-alive>
        </router-view>
      </n-layout-content>
    </n-layout>

    <!-- 底部状态栏：全局「现在正在发生什么」（详情进任务中心） -->
    <div class="status-bar">
      <n-space align="center" size="small" wrap>
        <template v-if="waitingJobs.length">
          <n-button size="tiny" quaternary type="warning" @click="openFirstWaiting">
            ⚠ {{ waitingJobs.length }} 个任务等待确认
          </n-button>
        </template>
        <template v-else-if="activeCount">
          <n-tag size="tiny" type="info">{{ activeCount }} 进行中</n-tag>
          <n-text depth="3" style="font-size: 12px">{{ runningSummary || '后台运行中' }}</n-text>
        </template>
        <n-text v-else depth="3" style="font-size: 12px">就绪</n-text>
        <n-button v-if="failedInView.length" size="tiny" quaternary
                  class="rt-error" @click="openJobsDrawer">
          {{ failedInView.length }} 个任务失败
        </n-button>
        <n-button v-if="session.interruptedJobs" size="tiny" quaternary
                  class="rt-warning" @click="openJobsDrawer">
          {{ session.interruptedJobs }} 个任务被中断
        </n-button>
      </n-space>
      <span style="flex: 1" />
      <n-text depth="3" style="font-size: 11px">Ctrl+J</n-text>
      <n-button size="tiny" quaternary @click="openJobsDrawer">任务中心</n-button>
    </div>

    <!-- 任务中心抽屉 -->
    <jobs-drawer v-model:show="jobsDrawerOpen" />
  </n-layout>

  <!-- 自动重启遮罩（迁移后清理占用文件）：服务恢复后自动刷新页面 -->
  <n-modal :show="restarting" preset="card" title="正在重启服务" style="width: min(400px, 96vw)"
           :mask-closable="false" :closable="false">
    <n-space vertical>
      <span class="rt-fs13 rt-t2">
        为释放被占用的文件并完成迁移清理，服务正在自动重启。<br>
        恢复后页面会自动刷新，请稍候...
      </span>
    </n-space>
  </n-modal>

  <!-- 服务停止遮罩（浏览器模式；GUI 窗口由看门狗自动关闭） -->
  <n-modal :show="serverDown && !restarting" preset="card" title="服务已停止" style="width: min(400px, 96vw)"
           :mask-closable="false" :closable="false">
    <n-space vertical>
      <span class="rt-fs13 rt-t2">
        后台服务已退出（托盘「退出服务」或 --mode stop）。<br>
        重新启动服务后点击下方按钮重连。
      </span>
      <n-button type="primary" @click="heartbeat">重新连接</n-button>
    </n-space>
  </n-modal>

  <!-- 更新可用弹窗（手动/自动检查命中；下载在 GitHub Release 页） -->
  <n-modal v-model:show="updateVisible" preset="card" title="发现新版本" style="width: min(520px, 96vw)">
    <n-space vertical size="medium">
      <n-text>当前版本 v{{ updateInfo?.current }}，最新版本 {{ updateInfo?.latest }}</n-text>
      <n-text depth="3" style="font-size: 12px">
        前往 GitHub Release 下载最新安装包，直接安装即可沿用原路径与数据。
      </n-text>
      <div v-if="updateInfo?.notes">
        <div style="font-weight: 600; margin: 8px 0 4px">更新说明</div>
        <div class="modal-scroll rt-fs13 rt-t2" style="white-space: pre-line">
          {{ updateInfo.notes }}
        </div>
      </div>
    </n-space>
    <template #footer>
      <n-space justify="end">
        <n-button @click="updateVisible = false">稍后再说</n-button>
        <n-button type="primary" @click="gotoDownload">前往下载</n-button>
      </n-space>
    </template>
  </n-modal>

  <!-- 关于（版本 + 自动检查开关 + 手动检查入口） -->
  <n-modal v-model:show="aboutVisible" preset="card" title="关于" style="width: min(420px, 96vw)">
    <n-space vertical size="medium">
      <n-text>
        Ren'Py 翻译工具
        <n-text v-if="appVersion" depth="3">v{{ appVersion }}</n-text>
      </n-text>
      <n-text depth="3" style="font-size: 12px">
        AI 翻译 + 手动校对的 Ren'Py 视觉小说汉化工具。
      </n-text>
      <n-space align="center">
        <n-text depth="3" style="font-size: 12px">启动时自动检查更新</n-text>
        <n-switch size="small" :value="autoUpdateCheck"
                  @update:value="onAutoCheckChange" />
      </n-space>
      <n-button size="small" :loading="checkingUpdate" @click="checkUpdate(false)">
        立即检查更新
      </n-button>
    </n-space>
  </n-modal>

  <!-- 全局上传进度对话框（zip 传输阶段；顶栏任务列表点开） -->
  <upload-progress-dialog />

  <!-- 全局任务进度对话框（刷新/切路由后自动重开） -->
  <job-progress-dialog v-for="j in dialogJobs" :key="j.id" :job="j">
    <template #question="{ question }">
      <div v-if="question && question.type === 'confirm'" style="border-top: 1px solid var(--rt-border); padding-top: 12px">
        <div style="font-weight: 600; margin-bottom: 6px">{{ question.payload.title }}</div>
        <div class="rt-fs13 rt-t2" style="white-space: pre-line">{{ question.payload.body }}</div>
        <n-space justify="end" style="margin-top: 10px">
          <!-- 官中三动作：keep_label 存在时展示 取消/保留/删除 -->
          <template v-if="question.payload.keep_label">
            <n-button size="small" @click="answerConfirm(j, 'cancel')">取消</n-button>
            <n-button size="small" type="primary" @click="answerConfirm(j, 'keep')">
              {{ question.payload.keep_label }}
            </n-button>
            <n-button size="small" type="error" @click="answerConfirm(j, 'delete')">删除官中</n-button>
          </template>
          <template v-else>
            <n-button size="small" @click="answerConfirm(j, false)">取消</n-button>
            <n-button size="small" type="error" @click="answerConfirm(j, true)">确认</n-button>
          </template>
        </n-space>
      </div>
      <embedded-review-dialog
        v-else-if="question && question.type === 'embedded_review'"
        :job="j" :question="question"
      />
    </template>
  </job-progress-dialog>
</template>

<style scoped>
/* 底部状态栏：常驻 26px，全局「现在正在发生什么」 */
.status-bar {
  position: fixed;
  left: 180px;
  right: 0;
  bottom: 0;
  height: 26px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 10px;
  font-size: 12px;
  background: var(--n-color, var(--rt-surface1));
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  z-index: 200;
}
</style>
