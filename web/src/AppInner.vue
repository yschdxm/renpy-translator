<script setup lang="ts">
/** 应用主体（在 message/dialog provider 内，可用 useMessage） */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NLayout, NLayoutHeader, NLayoutSider, NLayoutContent, NMenu, NModal, NSelect,
  NTag, NButton, NDropdown, NSpace, useMessage,
} from 'naive-ui'
import type { MenuOption } from 'naive-ui'
import { useSessionStore } from './stores/session'
import { useProjectsStore } from './stores/projects'
import { useJobsStore, type JobView } from './stores/jobs'
import JobProgressDialog from './components/JobProgressDialog.vue'
import EmbeddedReviewDialog from './components/EmbeddedReviewDialog.vue'
import { errorText } from './api/client'

const message = useMessage()
const session = useSessionStore()
const projectsStore = useProjectsStore()
const jobsStore = useJobsStore()
const route = useRoute()
const router = useRouter()

const menuOptions: MenuOption[] = [
  { label: '项目管理', key: '/projects' },
  { label: '人名翻译', key: '/names' },
  { label: '字符串翻译', key: '/strings' },
  { label: '对话翻译', key: '/dialogue' },
  { label: '导出游戏', key: '/export' },
  { label: '模型配置', key: '/settings' },
]

const activeKey = computed(() => route.path)

const projectOptions = computed(() =>
  projectsStore.list.map((p) => ({ label: p.name, value: p.name })))

/** 打开对话框的任务（含刷新后恢复的） */
const dialogJobs = computed(() =>
  [...jobsStore.jobs.values()].filter((j) => j.dialogOpen))

/** 后台活跃任务：关掉进度对话框后从顶栏任务列表找回 */
const backgroundJobs = computed(() =>
  jobsStore.activeJobs.filter((j) => !j.dialogOpen))

const taskMenuOptions = computed(() =>
  backgroundJobs.value.map((j) => ({
    key: j.id,
    label: j.status === 'waiting_input'
      ? `${j.label || '任务'} — 等待确认`
      : `${j.label || '任务'} ${Math.round(j.progress * 100)}%`,
  })))

function openFromList(jobId: string) {
  jobsStore.openDialog(jobId)
}

async function onProjectChange(name: string | null) {
  if (!name) return
  await session.open(name)
  await projectsStore.refresh()
}

function onMenu(key: string) {
  router.push(key)
}

/** 通用 confirm 问题回答 */
async function answerConfirm(job: JobView, ok: boolean) {
  const q = job.question
  if (!q) return
  try {
    await jobsStore.answer(job.id, q.question_id, { ok })
  } catch (e) {
    message.error(errorText(e), { duration: 8000 })
  }
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

  heartbeatTimer = setInterval(heartbeat, 5000)
  await session.refresh()
  await projectsStore.refresh()
  await jobsStore.restore()
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
})
</script>

<template>
  <n-layout style="height: 100vh">
    <n-layout-header bordered style="padding: 0 16px; height: 52px; display: flex; align-items: center; gap: 16px">
      <span style="font-size: 16px; font-weight: 600">Ren'Py 翻译工具</span>
      <n-select
        style="width: 260px"
        size="small"
        placeholder="选择项目"
        :value="session.currentProject || null"
        :options="projectOptions"
        @update:value="onProjectChange"
      />
      <n-tag v-if="session.currentProject" size="small" type="success">
        {{ session.progressText }}
      </n-tag>
      <n-tag v-if="session.currentProject && !session.hasTranslator" size="small" type="warning">
        未配置模型
      </n-tag>
      <!-- 后台任务列表：进度对话框被关闭后任务仍在跑，点列表项重开对应对话框 -->
      <span style="flex: 1" />
      <n-dropdown
        v-if="backgroundJobs.length"
        trigger="click" placement="bottom-end"
        :options="taskMenuOptions"
        @select="openFromList"
      >
        <n-button size="tiny" quaternary type="info">
          ⏳ 后台任务 {{ backgroundJobs.length }}
        </n-button>
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
      <n-layout-content content-style="padding: 16px">
        <!-- key=path+项目：/strings 与 /dialogue 共用 TextsPage 组件，
             不加 key 切换路由时组件实例被复用，表格不会重新加载；
             同理，切换当前项目后页面也必须重挂载以加载新项目数据 -->
        <router-view :key="$route.path + '|' + session.currentProject" />
      </n-layout-content>
    </n-layout>
  </n-layout>

  <!-- 自动重启遮罩（迁移后清理占用文件）：服务恢复后自动刷新页面 -->
  <n-modal :show="restarting" preset="card" title="正在重启服务" style="width: 400px"
           :mask-closable="false" :closable="false">
    <n-space vertical>
      <span style="font-size: 13px; color: #ccc">
        为释放被占用的文件并完成迁移清理，服务正在自动重启。<br>
        恢复后页面会自动刷新，请稍候...
      </span>
    </n-space>
  </n-modal>

  <!-- 服务停止遮罩（浏览器模式；GUI 窗口由看门狗自动关闭） -->
  <n-modal :show="serverDown && !restarting" preset="card" title="服务已停止" style="width: 400px"
           :mask-closable="false" :closable="false">
    <n-space vertical>
      <span style="font-size: 13px; color: #ccc">
        后台服务已退出（托盘「退出服务」或 --mode stop）。<br>
        重新启动服务后点击下方按钮重连。
      </span>
      <n-button type="primary" @click="heartbeat">重新连接</n-button>
    </n-space>
  </n-modal>

  <!-- 全局任务进度对话框（刷新/切路由后自动重开） -->
  <job-progress-dialog v-for="j in dialogJobs" :key="j.id" :job="j">
    <template #question="{ question }">
      <div v-if="question && question.type === 'confirm'" style="border-top: 1px solid #333; padding-top: 12px">
        <div style="font-weight: 600; margin-bottom: 6px">{{ question.payload.title }}</div>
        <div style="font-size: 13px; color: #ccc; white-space: pre-line">{{ question.payload.body }}</div>
        <n-space justify="end" style="margin-top: 10px">
          <n-button size="small" @click="answerConfirm(j, false)">取消</n-button>
          <n-button size="small" type="error" @click="answerConfirm(j, true)">确认</n-button>
        </n-space>
      </div>
      <embedded-review-dialog
        v-else-if="question && question.type === 'embedded_review'"
        :job="j" :question="question"
      />
    </template>
  </job-progress-dialog>
</template>
