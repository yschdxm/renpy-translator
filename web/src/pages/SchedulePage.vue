<script setup lang="ts">
/** 定时任务：倒计时 / 时间点 / 时间段（循环独立设置），指定项目独立执行 */
import { computed, h, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref } from 'vue'
import {
  NButton, NCheckbox, NDataTable, NDatePicker, NDivider, NEmpty, NInput,
  NInputNumber, NModal, NPopconfirm, NRadioButton, NRadioGroup, NSelect,
  NSpace, NSwitch, NText, NTimePicker, useMessage,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { RefreshOutline } from '@vicons/ionicons5'
import { api, toastError, toastOk } from '../api/client'
import { renderIcon } from '../components/icons'
import PageHeader from '../components/PageHeader.vue'
import { useProjectsStore } from '../stores/projects'
import { useSessionStore } from '../stores/session'
import {
  KIND_META, REPEAT_OPTIONS, defaultParams, kindLabel, type ParamKind,
  type ScheduleType,
} from '../lib/scheduledKinds'
import { usePageScroll } from '../composables/usePageScroll'

interface SchedTask {
  id: string
  name: string
  kind: string
  project: string
  params: Record<string, unknown>
  schedule_type: ScheduleType
  schedule: Record<string, unknown>
  repeat_policy: 'once' | 'daily'
  enabled: boolean
  armed_at?: string
  last_job_id?: string
  last_status?: string
  last_fired_at?: string
  next_run_at?: string | null
}
const message = useMessage()
const projects = useProjectsStore()
const session = useSessionStore()

const tasks = ref<SchedTask[]>([])
const nowMs = ref(Date.now())
const listLoading = ref(false)
let ticker: number | undefined
let autoTimer: number | undefined

async function load(silent = false) {
  if (silent) {
    // 静默轮询：失败不打扰（页面可见时每30s与调度器同步一次）
    try {
      const data = await api.get<{ tasks: SchedTask[] }>('/api/scheduled')
      tasks.value = data.tasks
    } catch { /* 忽略 */ }
    return
  }
  listLoading.value = true
  try {
    const data = await api.get<{ tasks: SchedTask[] }>('/api/scheduled')
    tasks.value = data.tasks
  } catch (e) {
    toastError(message, e)
  } finally {
    listLoading.value = false
  }
}

onMounted(() => {
  void load()
})
// keep-alive：定时器随激活启停（隐藏页不后台轮询）
onActivated(() => {
  ticker = window.setInterval(() => { nowMs.value = Date.now() }, 15000)
  // 与后端30s调度周期同步：任务触发/停用后列表状态自动可见
  autoTimer = window.setInterval(() => { void load(true) }, 30000)
})
onDeactivated(() => {
  window.clearInterval(ticker)
  window.clearInterval(autoTimer)
})
onUnmounted(() => {
  window.clearInterval(ticker)
  window.clearInterval(autoTimer)
})
usePageScroll(() => load(true))

function scheduleSummary(t: SchedTask): string {
  if (t.schedule_type === 'delay') {
    return `${t.schedule.minutes} 分钟后（一次性）`
  }
  if (t.schedule_type === 'at') {
    const at = new Date(String(t.schedule.at))
    const hm = at.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    return `${hm}${t.repeat_policy === 'daily' ? '（每天）' : '（一次性）'}`
  }
  return `${t.repeat_policy === 'daily' ? '每天' : '今天'} ${t.schedule.start}–${t.schedule.end}`
}

function fmtNext(iso?: string | null): string {
  if (!iso) return '—'
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return '—'
  const diff = t - nowMs.value
  const abs = new Date(t).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
  if (diff <= 0) return abs
  const m = Math.round(diff / 60000)
  if (m < 60) return `${m} 分钟后`
  const hh = Math.floor(m / 60)
  if (hh < 24) return `${hh} 小时 ${m % 60} 分后`
  return `${Math.floor(hh / 24)} 天后（${abs}）`
}
const formVisible = ref(false)
const editingId = ref('')
const form = reactive({
  name: '',
  kind: 'texts.translate-all.dialogue',
  project: '',
  params: {} as Record<string, unknown>,
  schedule_type: 'delay' as ScheduleType,
  repeat_policy: 'once' as 'once' | 'daily',
  delayMinutes: 30,
  delayUnit: 1,
  atTs: Date.now() + 3600_000,
  winStart: '01:00',
  winEnd: '05:00',
})

const paramKind = computed<ParamKind>(() => KIND_META[form.kind]?.paramKind ?? 'none')

function applyKindDefaults(kind: string) {
  form.kind = kind
  form.params = { ...defaultParams(KIND_META[kind]?.paramKind ?? 'none') }
}

function openCreate() {
  editingId.value = ''
  form.name = ''
  form.project = session.currentProject || projects.list[0]?.name || ''
  applyKindDefaults('texts.translate-all.dialogue')
  form.schedule_type = 'delay'
  form.repeat_policy = 'once'
  form.delayMinutes = 30
  form.delayUnit = 1
  form.atTs = Date.now() + 3600_000
  form.winStart = '01:00'
  form.winEnd = '05:00'
  formVisible.value = true
}

function openEdit(t: SchedTask) {
  editingId.value = t.id
  form.name = t.name
  form.kind = t.kind
  form.project = t.project
  form.params = { ...t.params }
  form.schedule_type = t.schedule_type
  form.repeat_policy = t.repeat_policy
  if (t.schedule_type === 'delay') {
    form.delayMinutes = Number(t.schedule.minutes || 30)
    form.delayUnit = 1
  } else if (t.schedule_type === 'at') {
    form.atTs = Date.parse(String(t.schedule.at))
  } else {
    form.winStart = String(t.schedule.start || '01:00')
    form.winEnd = String(t.schedule.end || '05:00')
  }
  formVisible.value = true
}

function buildSchedule(): { type: ScheduleType; schedule: Record<string, unknown>; repeat: 'once' | 'daily' } {
  if (form.schedule_type === 'delay') {
    return { type: 'delay', schedule: { minutes: form.delayMinutes * form.delayUnit }, repeat: 'once' }
  }
  if (form.schedule_type === 'at') {
    return { type: 'at', schedule: { at: new Date(form.atTs).toISOString() },
             repeat: form.repeat_policy }
  }
  return { type: 'window', schedule: { start: form.winStart, end: form.winEnd },
           repeat: form.repeat_policy }
}

async function submitForm() {
  if (!form.name.trim()) {
    message.warning('请填写任务名称')
    return
  }
  if (!form.project) {
    message.warning('请选择目标项目')
    return
  }
  const { type, schedule, repeat } = buildSchedule()
  // 已过时间禁止设置/开启
  if (type === 'at') {
    const at = new Date(form.atTs).getTime()
    if (at <= Date.now() + 5000) {
      message.warning('触发时间必须晚于当前时刻（已过的时间不能设置）')
      return
    }
  }
  if (type === 'window' && repeat === 'once') {
    const [eh, em] = String(schedule.end).split(':').map(Number)
    const endToday = new Date()
    endToday.setHours(eh, em, 0, 0)
    if (endToday.getTime() <= Date.now()) {
      message.warning('「仅一次」的时间段限今天：结束时间需晚于当前时刻')
      return
    }
  }
  const payload: Record<string, unknown> = {
    name: form.name.trim(), params: form.params,
    schedule_type: type, schedule, repeat_policy: repeat,
  }
  // 每次改期 = 从保存时刻重新计时（delay/仅一次窗口的「今天」基准）
  if (type === 'delay' || (type === 'window' && repeat === 'once')) {
    payload.armed_at = new Date().toISOString()
  }
  try {
    if (editingId.value) {
      await api.put(`/api/scheduled/${editingId.value}`, payload)
      toastOk(message, '定时任务已更新')
    } else {
      await api.post('/api/scheduled', {
        kind: form.kind, project: form.project, ...payload,
      })
      toastOk(message, '定时任务已创建')
    }
    formVisible.value = false
    await load()
  } catch (e) {
    toastError(message, e)
  }
}

async function runNow(t: SchedTask) {
  try {
    const data = await api.post<{ job_id: string | null; message: string }>(
      `/api/scheduled/${t.id}/run-now`)
    message.info(data.message)
    await load()
  } catch (e) {
    toastError(message, e)
  }
}

async function removeTask(t: SchedTask) {
  try {
    await api.del(`/api/scheduled/${t.id}`)
    toastOk(message, '定时任务已删除')
    await load()
  } catch (e) {
    toastError(message, e)
  }
}

async function toggleTask(t: SchedTask, v: boolean) {
  try {
    // 重新启用 = 从现在重新计时（否则沿用旧 armed_at 会导致
    // 下次运行时间落在过去/错过，任务看起来永远不执行）
    const body: Record<string, unknown> = { enabled: v }
    if (v && t.schedule_type === 'delay') {
      body.armed_at = new Date().toISOString()
    }
    await api.put(`/api/scheduled/${t.id}`, body)
    await load()
  } catch (e) {
    toastError(message, e)
  }
}

const projectOptions = computed(() =>
  projects.list.map((p) => ({ label: p.name, value: p.name })))
const kindOptions = computed(() =>
  Object.keys(KIND_META).map((k) => ({ label: kindLabel(k), value: k })))
const columns = computed<DataTableColumns<SchedTask>>(() => [
  { title: '任务名', key: 'name', ellipsis: { tooltip: true } },
  { title: '类型', key: 'kind', width: 130, render: (t) => kindLabel(t.kind) },
  { title: '项目', key: 'project', width: 160, ellipsis: { tooltip: true } },
  { title: '调度', key: 'schedule_type', width: 160, render: (t) => scheduleSummary(t) },
  { title: '下次运行', key: 'next_run_at', width: 130,
    render: (t) => fmtNext(t.next_run_at) },
  { title: '上次', key: 'last_status', width: 100, ellipsis: { tooltip: true },
    render: (t) => t.last_status || '—' },
  {
    title: '启用', key: 'enabled', width: 60,
    render: (t) => h(NSwitch, {
      size: 'small', value: t.enabled,
      'onUpdate:value': (v: boolean) => toggleTask(t, v),
    }),
  },
  {
    title: '操作', key: 'op', width: 150,
    render: (t) => h(NSpace, { size: 4 }, () => [
      h(NButton, { size: 'tiny', type: 'primary', quaternary: true,
                   onClick: () => runNow(t) }, () => '立即执行'),
      h(NButton, { size: 'tiny', quaternary: true,
                   onClick: () => openEdit(t) }, () => '编辑'),
      h(NPopconfirm, { onPositiveClick: () => removeTask(t) }, {
        trigger: () => h(NButton, { size: 'tiny', type: 'error', quaternary: true }, () => '删除'),
        default: () => '确定删除该定时任务？',
      }),
    ]),
  },
])
</script>

<template>
  <div>
    <page-header title="定时任务" help-section="15">
      <template #actions>
        <n-button size="small" :loading="listLoading" :render-icon="renderIcon(RefreshOutline)"
                  @click="load()">刷新</n-button>
        <n-button size="small" type="primary" @click="openCreate">新建定时任务</n-button>
      </template>
    </page-header>

    <n-text depth="3" style="display: block; margin-bottom: 12px; font-size: 12px">
      到点为目标项目独立执行（不依赖当前打开的页面）；错过时间点/窗口不补跑，
      时间段结束会取消进行中的任务（当前批次收尾）。
    </n-text>

    <n-data-table :columns="columns" :data="tasks" size="small"
                  :row-key="(r: SchedTask) => r.id"
                  :render-empty="() => h(NEmpty, { description: '还没有定时任务' })" />

    <n-modal v-model:show="formVisible" preset="card"
             :title="editingId ? '编辑定时任务' : '新建定时任务'"
             style="width: min(560px, 96vw)">
      <n-space vertical size="small">
        <!-- 执行内容 -->
        <n-divider title-placement="left" style="margin: 4px 0 2px">执行内容</n-divider>
        <n-input v-model:value="form.name" placeholder="任务名称（如：半夜批量翻译）" />
        <n-space align="center">
          <n-text class="fld-label">目标项目</n-text>
          <n-select v-model:value="form.project" :options="projectOptions"
                    style="flex: 1" placeholder="到点后独立执行的项目" />
        </n-space>
        <n-space align="center">
          <n-text class="fld-label">任务类型</n-text>
          <n-select :value="form.kind" :options="kindOptions" style="flex: 1"
                    @update:value="applyKindDefaults" />
        </n-space>
        <n-space v-if="paramKind === 'scope'" align="center" wrap>
          <n-text class="fld-label">范围</n-text>
          <n-select :value="String(form.params.filter_mode || 'all')" style="width: 110px"
                    :options="[
                      { label: '全部', value: 'all' },
                      { label: '仅未翻译', value: 'untranslated' },
                      { label: '仅已翻译', value: 'translated' },
                    ]"
                    @update:value="(v: string) => { form.params.filter_mode = v }" />
          <n-input v-model:value="(form.params.character as string)" size="small"
                   style="width: 130px" placeholder="角色变量名（可选）" />
          <n-input v-model:value="(form.params.search as string)" size="small"
                   style="flex: 1; min-width: 140px" placeholder="搜索过滤（可选）" />
        </n-space>
        <n-space v-else-if="paramKind === 'mode'" align="center">
          <n-text class="fld-label">模式</n-text>
          <n-radio-group :value="String(form.params.mode || 'both')" size="small"
                         @update:value="(v: string) => { form.params.mode = v }">
            <n-radio-button value="both">翻译+分析</n-radio-button>
            <n-radio-button value="name">仅翻译</n-radio-button>
            <n-radio-button value="analyze">仅分析</n-radio-button>
          </n-radio-group>
        </n-space>
        <n-space v-else-if="paramKind === 'shell'" align="center">
          <n-text class="fld-label">导出</n-text>
          <n-checkbox :checked="Boolean(form.params.shell)"
                      @update:checked="(v: boolean) => { form.params.shell = v }">
            套 macOS 应用壳（无壳项目忽略）
          </n-checkbox>
        </n-space>
        <n-space v-else-if="paramKind === 'chunk'" align="center">
          <n-text class="fld-label">每批</n-text>
          <n-input-number :value="Number(form.params.chunk_size || 10)" :min="1" :max="50"
                          style="width: 90px"
                          @update:value="(v: number | null) => { form.params.chunk_size = v ?? 10 }" />
          <n-text depth="3" style="font-size: 12px">条</n-text>
        </n-space>

        <!-- 执行计划 -->
        <n-divider title-placement="left" style="margin: 4px 0 2px">执行计划</n-divider>
        <n-radio-group v-model:value="form.schedule_type" size="small">
          <n-radio-button value="delay">倒计时</n-radio-button>
          <n-radio-button value="at">时间点</n-radio-button>
          <n-radio-button value="window">时间段</n-radio-button>
        </n-radio-group>

        <n-space v-if="form.schedule_type === 'delay'" align="center">
          <n-text class="fld-label">触发于</n-text>
          <n-input-number v-model:value="form.delayMinutes" :min="1" style="width: 90px" />
          <n-select :value="form.delayUnit" style="width: 90px"
                    :options="[
                      { label: '分钟后', value: 1 }, { label: '小时后', value: 60 },
                      { label: '天后', value: 1440 },
                    ]"
                    @update:value="(v: number) => { form.delayUnit = v }" />
          <n-text depth="3" style="font-size: 12px">之后触发一次，自动停用</n-text>
        </n-space>

        <n-space v-else-if="form.schedule_type === 'at'" align="center" wrap>
          <n-text class="fld-label">触发于</n-text>
          <n-date-picker v-model:value="form.atTs" type="datetime" style="width: 220px"
                         :is-date-disabled="(ts: number) => ts < Date.now()" />
        </n-space>

        <n-space v-else align="center" wrap>
          <n-text class="fld-label">每天窗口</n-text>
          <n-time-picker v-model:formatted-value="form.winStart" format="HH:mm"
                         :clearable="false" style="width: 100px" />
          <n-text depth="3">–</n-text>
          <n-time-picker v-model:formatted-value="form.winEnd" format="HH:mm"
                         :clearable="false" style="width: 100px" />
        </n-space>

        <n-space align="center">
          <n-text class="fld-label">循环</n-text>
          <n-select :value="form.schedule_type === 'delay' ? 'once' : form.repeat_policy"
                    :disabled="form.schedule_type === 'delay'" style="width: 110px"
                    :options="REPEAT_OPTIONS"
                    @update:value="(v: 'once' | 'daily') => { form.repeat_policy = v }" />
          <n-text depth="3" style="font-size: 12px">
            {{ form.schedule_type === 'delay' ? '倒计时恒为一次'
               : form.schedule_type === 'window' ? '窗口结束自动取消进行中的任务'
               : '' }}
          </n-text>
        </n-space>

        <n-text depth="3" style="font-size: 12px">
          到点为目标项目独立执行；错过时间点/窗口不补跑。
        </n-text>
      </n-space>
      <template #footer>
        <n-space justify="end">
          <n-button @click="formVisible = false">取消</n-button>
          <n-button type="primary" @click="submitForm">
            {{ editingId ? '保存' : '创建' }}
          </n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
/* 表单左标签列：全表单统一宽度，避免行与行错位 */
.fld-label {
  width: 64px;
  flex: none;
  font-size: 12px;
}
</style>
