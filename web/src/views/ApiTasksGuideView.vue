<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import MarkdownContent from '@/components/MarkdownContent.vue'
import { errorMessage } from '@/utils/errors'

const { t } = useI18n()
// Agent 版随管理台作为公开静态文件发布：链接可以直接交给 Agent 自己去读。
const AGENT_PATH = '/integration/api-tasks-agent-guide.md'
const PLACEHOLDER = '{{COREMAN_BASE_URL}}'
const drawer = ref(false)
const raw = ref('')
const filled = computed(() => raw.value.split(PLACEHOLDER).join(window.location.origin))

async function openAgent() {
  drawer.value = true
  if (raw.value) return
  try {
    const response = await fetch(AGENT_PATH)
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    raw.value = await response.text()
  } catch (e) { ElMessage.error(errorMessage(e)) }
}
async function copy(text: string) {
  try { await navigator.clipboard.writeText(text); ElMessage.success(t('apiTasksGuide.agentDrawer.copied')) }
  catch (e) { ElMessage.error(errorMessage(e)) }
}
const copyAll = () => copy(filled.value)
const copyLink = () => copy(new URL(AGENT_PATH, window.location.origin).href)

const g = (key: string) => t(`apiTasksGuide.${key}`)
const lanes = [
  { x: 10, w: 210, key: 'overview.caller' },
  { x: 240, w: 480, key: 'overview.coreman' },
  { x: 740, w: 210, key: 'overview.feishu' },
]
const core = [
  { y: 70, title: 'overview.api', sub: 'overview.apiSub' },
  { y: 150, title: 'overview.check', sub: 'overview.checkSub' },
  { y: 230, title: 'overview.queue', sub: 'overview.queueSub' },
  { y: 310, title: 'overview.run', sub: 'overview.runSub' },
]
const chatSide = [
  { y: 90, key: 'overview.trigger', kind: 'api' },
  { y: 200, key: 'overview.reply', kind: 'api' },
  { y: 310, key: 'overview.human', kind: 'human' },
]
const lifelines = [
  { x: 110, key: 'sequence.caller' },
  { x: 370, key: 'sequence.coreman' },
  { x: 610, key: 'sequence.bot' },
  { x: 850, key: 'sequence.feishu' },
]
const messages = [
  { from: 0, to: 1, y: 110, key: 'sequence.a1' },
  { from: 1, to: 0, y: 150, key: 'sequence.a2', dashed: true },
  { from: 1, to: 3, y: 190, key: 'sequence.a3' },
  { from: 1, to: 2, y: 230, key: 'sequence.a4' },
  { from: 2, to: 3, y: 270, key: 'sequence.a5' },
  { from: 0, to: 1, y: 320, key: 'sequence.a6' },
  { from: 1, to: 0, y: 360, key: 'sequence.a7', dashed: true },
  { from: 3, to: 2, y: 420, key: 'sequence.a8', dashed: true, human: true },
]
const terminal = [
  { y: 16, key: 'succeeded', tone: 'ok' },
  { y: 66, key: 'waiting', tone: 'warn' },
  { y: 116, key: 'superseded', tone: 'human' },
  { y: 166, key: 'failed', tone: 'bad' },
  { y: 216, key: 'cancelled', tone: 'muted' },
]
const legend = [
  { key: 'queued', code: 'queued', hint: 'hintQueued', tone: 'muted' },
  { key: 'waiting', code: 'waiting_user', hint: 'hintWaiting', tone: 'warn' },
  { key: 'superseded', code: 'superseded', hint: 'hintSuperseded', tone: 'human' },
  { key: 'failed', code: 'failed', hint: 'hintFailed', tone: 'bad' },
  { key: 'cancelled', code: 'cancelled', hint: 'hintCancelled', tone: 'muted' },
]
const chat = [
  { n: 1, who: 'user', text: 'chat.m1', mention: true },
  { n: 2, who: 'bot', text: 'chat.m2' },
  { n: 3, who: 'bot', title: 'chat.m3Title', text: 'chat.m3', quote: true, group: 'api' },
  { n: 4, who: 'bot', text: 'chat.m4', group: 'api' },
  { n: 5, who: 'user', text: 'chat.m5', mention: true, group: 'human' },
  { n: 6, who: 'bot', text: 'chat.m6', group: 'human' },
]
</script>

<template>
  <section class="guide">
    <header class="head">
      <h1>{{ g('title') }}</h1>
      <p class="intro">
        {{ g('intro') }}
      </p>
      <div class="actions">
        <el-button
          type="primary"
          data-test="agent-version"
          @click="openAgent"
        >
          {{ g('agentVersion') }}
        </el-button>
        <el-button @click="$router.push('/my-api-tokens')">
          {{ g('goTokens') }}
        </el-button>
      </div>
    </header>

    <article class="block">
      <h2>{{ g('overview.title') }}</h2>
      <div class="figure">
        <svg
          viewBox="0 0 960 400"
          role="img"
          :aria-label="g('overview.title')"
        >
          <defs>
            <marker
              id="guide-arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            ><path
              class="arrow-head"
              d="M0,0 L10,5 L0,10 z"
            /></marker>
            <marker
              id="guide-arrow-human"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            ><path
              class="arrow-head human"
              d="M0,0 L10,5 L0,10 z"
            /></marker>
          </defs>
          <g
            v-for="lane in lanes"
            :key="lane.key"
          >
            <rect
              class="lane"
              :x="lane.x"
              y="40"
              :width="lane.w"
              height="352"
              rx="12"
            />
            <text
              class="lane-title"
              :x="lane.x + lane.w / 2"
              y="28"
            >{{ g(lane.key) }}</text>
          </g>
          <rect
            class="node"
            x="25"
            y="70"
            width="180"
            height="100"
            rx="10"
          />
          <text
            class="node-title"
            x="115"
            y="112"
          >{{ g('overview.caller') }}</text>
          <text
            class="node-sub"
            x="115"
            y="136"
          >{{ g('overview.callerSub') }}</text>
          <g
            v-for="(box, i) in core"
            :key="box.title"
          >
            <rect
              class="node core"
              x="360"
              :y="box.y"
              width="240"
              height="56"
              rx="10"
            />
            <text
              class="node-title"
              x="480"
              :y="box.y + 24"
            >{{ g(box.title) }}</text>
            <text
              class="node-sub"
              x="480"
              :y="box.y + 44"
            >{{ g(box.sub) }}</text>
            <line
              v-if="i < core.length - 1"
              class="edge"
              x1="480"
              :y1="box.y + 56"
              x2="480"
              :y2="box.y + 78"
              marker-end="url(#guide-arrow)"
            />
          </g>
          <g
            v-for="item in chatSide"
            :key="item.key"
          >
            <rect
              :class="['node', 'chat', item.kind]"
              x="755"
              :y="item.y"
              width="180"
              height="56"
              rx="10"
            />
            <text
              class="node-title small"
              x="845"
              :y="item.y + 33"
            >{{ g(item.key) }}</text>
          </g>
          <line
            class="edge"
            x1="205"
            y1="96"
            x2="358"
            y2="96"
            marker-end="url(#guide-arrow)"
          />
          <text
            class="edge-label"
            x="282"
            y="88"
          >{{ g('overview.step1') }}</text>
          <line
            class="edge dashed"
            x1="358"
            y1="114"
            x2="207"
            y2="146"
            marker-end="url(#guide-arrow)"
          />
          <text
            class="edge-label"
            x="282"
            y="152"
          >{{ g('overview.step4') }}</text>
          <line
            class="edge"
            x1="600"
            y1="178"
            x2="753"
            y2="120"
            marker-end="url(#guide-arrow)"
          />
          <text
            class="edge-label"
            x="676"
            y="138"
          >{{ g('overview.step2') }}</text>
          <line
            class="edge"
            x1="600"
            y1="330"
            x2="753"
            y2="232"
            marker-end="url(#guide-arrow)"
          />
          <text
            class="edge-label"
            x="676"
            y="268"
          >{{ g('overview.step3') }}</text>
          <line
            class="edge human"
            x1="755"
            y1="346"
            x2="602"
            y2="346"
            marker-end="url(#guide-arrow-human)"
          />
          <text
            class="edge-label human"
            x="678"
            y="370"
          >{{ g('overview.step5') }}</text>
        </svg>
      </div>
    </article>

    <article class="block">
      <h2>{{ g('setup.title') }}</h2>
      <ol class="steps">
        <li
          v-for="n in 3"
          :key="n"
        >
          <span class="badge">{{ n }}</span>
          <div>
            <strong>{{ g(`setup.s${n}Title`) }}</strong>
            <p>{{ g(`setup.s${n}`) }}</p>
          </div>
        </li>
      </ol>
    </article>

    <article class="block">
      <h2>{{ g('chat.title') }}</h2>
      <div class="phone">
        <div class="phone-head">
          {{ g('chat.group') }}
        </div>
        <div class="phone-body">
          <div
            v-for="m in chat"
            :key="m.n"
            :class="['msg', m.who, m.group ? `in-${m.group}` : '']"
          >
            <span
              v-if="m.n === 3 || m.n === 5"
              :class="['group-tag', m.group]"
            >{{ g(m.group === 'api' ? 'chat.apiTag' : 'chat.humanTag') }}</span>
            <span class="avatar">{{ g(m.who === 'user' ? 'chat.user' : 'chat.bot').slice(0, 1) }}</span>
            <div class="msg-main">
              <span class="name">{{ g(m.who === 'user' ? 'chat.user' : 'chat.bot') }}<span class="num">{{ m.n }}</span></span>
              <div class="bubble">
                <template v-if="m.mention">
                  <span class="mention">@{{ g('chat.bot') }}</span>&nbsp;
                </template>
                <strong v-if="m.title">{{ g(m.title) }}</strong>
                <blockquote v-if="m.quote">
                  {{ g(m.text) }}
                </blockquote>
                <template v-else>
                  {{ g(m.text) }}
                </template>
              </div>
            </div>
          </div>
        </div>
      </div>
      <p class="caption">
        {{ g('chat.caption') }}
      </p>
    </article>

    <article class="block">
      <h2>{{ g('sequence.title') }}</h2>
      <div class="figure">
        <svg
          viewBox="0 0 960 450"
          role="img"
          :aria-label="g('sequence.title')"
        >
          <g
            v-for="life in lifelines"
            :key="life.key"
          >
            <rect
              class="node core"
              :x="life.x - 80"
              y="16"
              width="160"
              height="40"
              rx="8"
            />
            <text
              class="node-title"
              :x="life.x"
              y="41"
            >{{ g(life.key) }}</text>
            <line
              class="lifeline"
              :x1="life.x"
              y1="56"
              :x2="life.x"
              y2="440"
            />
          </g>
          <rect
            class="phase human-phase"
            x="20"
            y="392"
            width="920"
            height="48"
            rx="8"
          />
          <g
            v-for="m in messages"
            :key="m.key"
          >
            <line
              :class="['edge', { dashed: m.dashed, human: m.human }]"
              :x1="lifelines[m.from].x + (m.to > m.from ? 4 : -4)"
              :y1="m.y"
              :x2="lifelines[m.to].x + (m.to > m.from ? -6 : 6)"
              :y2="m.y"
              :marker-end="m.human ? 'url(#guide-arrow-human)' : 'url(#guide-arrow)'"
            />
            <text
              :class="['edge-label', { human: m.human }]"
              :x="(lifelines[m.from].x + lifelines[m.to].x) / 2"
              :y="m.y - 8"
            >{{ g(m.key) }}</text>
          </g>
        </svg>
      </div>
    </article>

    <article class="block">
      <h2>{{ g('states.title') }}</h2>
      <div class="figure narrow">
        <svg
          viewBox="0 0 760 260"
          role="img"
          :aria-label="g('states.title')"
        >
          <rect
            class="state muted"
            x="20"
            y="106"
            width="150"
            height="44"
            rx="22"
          />
          <text
            class="node-title"
            x="95"
            y="133"
          >{{ g('states.queued') }}</text>
          <rect
            class="state run"
            x="250"
            y="106"
            width="150"
            height="44"
            rx="22"
          />
          <text
            class="node-title"
            x="325"
            y="133"
          >{{ g('states.running') }}</text>
          <line
            class="edge"
            x1="170"
            y1="128"
            x2="248"
            y2="128"
            marker-end="url(#guide-arrow)"
          />
          <g
            v-for="s in terminal"
            :key="s.key"
          >
            <path
              class="edge"
              :d="`M400,128 C450,128 450,${s.y + 20} 518,${s.y + 20}`"
              fill="none"
              marker-end="url(#guide-arrow)"
            />
            <rect
              :class="['state', s.tone]"
              x="520"
              :y="s.y"
              width="200"
              height="40"
              rx="20"
            />
            <text
              class="node-title"
              x="620"
              :y="s.y + 25"
            >{{ g(`states.${s.key}`) }}</text>
          </g>
        </svg>
      </div>
      <ul class="legend">
        <li
          v-for="item in legend"
          :key="item.key"
        >
          <span :class="['dot', item.tone]" />
          <strong>{{ g(`states.${item.key}`) }}</strong>
          <code>{{ item.code }}</code>
          <span>{{ g(`states.${item.hint}`) }}</span>
        </li>
      </ul>
    </article>

    <article class="block">
      <h2>{{ g('rules.title') }}</h2>
      <div class="cards">
        <div
          v-for="n in 4"
          :key="n"
          class="card"
        >
          <strong>{{ g(`rules.r${n}Title`) }}</strong>
          <p>{{ g(`rules.r${n}`) }}</p>
        </div>
      </div>
    </article>

    <el-drawer
      v-model="drawer"
      :title="g('agentDrawer.title')"
      size="min(760px, 100%)"
      append-to-body
    >
      <p class="intro">
        {{ g('agentDrawer.intro') }}
      </p>
      <div class="actions">
        <el-button
          type="primary"
          data-test="copy-agent-guide"
          :disabled="!raw"
          @click="copyAll"
        >
          {{ g('agentDrawer.copy') }}
        </el-button>
        <el-button @click="copyLink">
          {{ g('agentDrawer.copyLink') }}
        </el-button>
        <el-button
          tag="a"
          :href="AGENT_PATH"
          download
        >
          {{ g('agentDrawer.download') }}
        </el-button>
      </div>
      <MarkdownContent
        v-if="raw"
        data-test="agent-guide"
        :content="filled"
      />
    </el-drawer>
  </section>
</template>

<style scoped>
.guide { max-width: 1040px; }
.head h1 { margin-bottom: 8px; }
.intro { max-width: 78ch; color: var(--el-text-color-secondary); line-height: 1.75; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0; }
.actions .el-button + .el-button { margin-left: 0; }
.block { margin-top: 40px; }
.block h2 { font-size: 18px; margin: 0 0 16px; }
.figure { overflow-x: auto; padding: 16px; background: var(--el-bg-color); border: 1px solid var(--el-border-color-lighter); border-radius: 12px; }
.figure svg { display: block; width: 100%; min-width: 680px; height: auto; }
.figure.narrow svg { max-width: 760px; min-width: 520px; margin: 0 auto; }

.lane { fill: var(--el-fill-color-lighter); stroke: var(--el-border-color-lighter); }
.lane-title { fill: var(--el-text-color-secondary); font-size: 13px; font-weight: 600; text-anchor: middle; letter-spacing: .04em; }
.node { fill: var(--el-bg-color); stroke: var(--el-border-color); stroke-width: 1.2; }
.node.core { stroke: var(--el-color-primary-light-5); }
.node.chat.api { fill: var(--el-color-primary-light-9); stroke: var(--el-color-primary-light-5); }
.node.chat.human { fill: var(--el-color-success-light-9); stroke: var(--el-color-success-light-5); }
.node-title { fill: var(--el-text-color-primary); font-size: 14px; font-weight: 600; text-anchor: middle; }
.node-title.small { font-size: 13px; }
.node-sub { fill: var(--el-text-color-secondary); font-size: 12px; text-anchor: middle; }
.edge { stroke: var(--el-color-primary); stroke-width: 1.6; fill: none; }
.edge.dashed { stroke-dasharray: 6 5; }
.edge.human { stroke: var(--el-color-success); }
.arrow-head { fill: var(--el-color-primary); }
.arrow-head.human { fill: var(--el-color-success); }
.edge-label { fill: var(--el-color-primary); font-size: 12px; font-weight: 600; text-anchor: middle; paint-order: stroke; stroke: var(--el-bg-color); stroke-width: 4px; }
.edge-label.human { fill: var(--el-color-success); }
.lifeline { stroke: var(--el-border-color); stroke-dasharray: 3 4; }
.phase.human-phase { fill: var(--el-color-success-light-9); }
.state { stroke-width: 1.2; }
.state.muted { fill: var(--el-fill-color-light); stroke: var(--el-border-color); }
.state.run { fill: var(--el-color-primary-light-9); stroke: var(--el-color-primary-light-5); }
.state.ok { fill: var(--el-color-success-light-9); stroke: var(--el-color-success-light-5); }
.state.warn { fill: var(--el-color-warning-light-9); stroke: var(--el-color-warning-light-5); }
.state.human { fill: var(--el-color-success-light-9); stroke: var(--el-color-success); stroke-dasharray: 4 3; }
.state.bad { fill: var(--el-color-danger-light-9); stroke: var(--el-color-danger-light-5); }

.steps { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; padding: 0; margin: 0; list-style: none; }
.steps li { display: flex; gap: 12px; padding: 16px; border: 1px solid var(--el-border-color-lighter); border-radius: 12px; background: var(--el-bg-color); }
.steps p, .card p { margin: 6px 0 0; color: var(--el-text-color-secondary); line-height: 1.65; }
.badge { flex: none; display: inline-grid; place-items: center; width: 28px; height: 28px; border-radius: 50%; background: var(--el-color-primary); color: #fff; font-weight: 700; }

.phone { max-width: 560px; border: 1px solid var(--el-border-color); border-radius: 16px; overflow: hidden; background: var(--el-fill-color-lighter); }
.phone-head { padding: 12px 16px; font-weight: 600; background: var(--el-bg-color); border-bottom: 1px solid var(--el-border-color-lighter); }
.phone-body { display: flex; flex-direction: column; gap: 14px; padding: 16px; }
.msg { position: relative; display: flex; gap: 10px; }
.msg.in-api, .msg.in-human { padding: 10px 10px 10px 8px; border-left: 3px solid var(--el-color-primary); background: var(--el-color-primary-light-9); border-radius: 0 8px 8px 0; }
.msg.in-human { border-left-color: var(--el-color-success); background: var(--el-color-success-light-9); }
.group-tag { position: absolute; top: -10px; right: 8px; padding: 1px 8px; font-size: 12px; border-radius: 10px; color: #fff; background: var(--el-color-primary); }
.group-tag.human { background: var(--el-color-success); }
.avatar { flex: none; display: inline-grid; place-items: center; width: 32px; height: 32px; border-radius: 50%; font-size: 14px; color: #fff; background: var(--el-color-success); }
.msg.bot .avatar { background: var(--el-color-primary); }
.msg-main { min-width: 0; }
.name { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--el-text-color-secondary); }
.num { display: inline-grid; place-items: center; width: 16px; height: 16px; border-radius: 50%; font-size: 11px; background: var(--el-text-color-secondary); color: var(--el-bg-color); }
.bubble { margin-top: 4px; padding: 8px 12px; line-height: 1.6; border-radius: 4px 12px 12px 12px; background: var(--el-bg-color); box-shadow: 0 1px 2px rgb(0 0 0 / 6%); overflow-wrap: anywhere; }
.bubble blockquote { margin: 6px 0 0; padding-left: 10px; border-left: 3px solid var(--el-border-color); color: var(--el-text-color-secondary); }
.mention { color: var(--el-color-primary); }
.caption { max-width: 78ch; margin-top: 12px; color: var(--el-text-color-secondary); line-height: 1.7; }

.legend { display: grid; gap: 8px; padding: 0; margin: 16px 0 0; list-style: none; }
.legend li { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; color: var(--el-text-color-secondary); }
.legend strong { color: var(--el-text-color-primary); }
.dot { width: 10px; height: 10px; border-radius: 50%; border: 1px solid var(--el-border-color); background: var(--el-fill-color-light); }
.dot.warn { background: var(--el-color-warning-light-7); border-color: var(--el-color-warning); }
.dot.human { background: var(--el-color-success-light-7); border-color: var(--el-color-success); }
.dot.bad { background: var(--el-color-danger-light-7); border-color: var(--el-color-danger); }

.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
.card { padding: 16px; border: 1px solid var(--el-border-color-lighter); border-radius: 12px; background: var(--el-bg-color); }
</style>
