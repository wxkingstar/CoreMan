<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
const props = defineProps<{ covering: boolean }>()
const { t } = useI18n()
const scene = ref<SVGSVGElement>()
const looks = ref([{ x: 0, y: 0 }, { x: 0, y: 0 }, { x: 0, y: 0 }])
const characters = [
  { x: 89, y: 219, color: '#EF9279', shade: '#D67860', width: 180, height: 230, faceY: 79, angle: -8 },
  { x: 296, y: 76, color: '#16756B', shade: '#105E55', width: 182, height: 369, faceY: 122, angle: 4 },
  { x: 492, y: 199, color: '#B6A6D9', shade: '#9787BA', width: 166, height: 250, faceY: 91, angle: 9 },
]
// One animation value drives both wrists and arms, so they stay joined in every frame.
const lift = ref(props.covering ? 1 : 0)
let armFrame = 0
watch(() => props.covering, (covered) => {
  cancelAnimationFrame(armFrame)
  const target = covered ? 1 : 0
  if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) { lift.value = target; return }
  const start = performance.now(), from = lift.value
  const animate = (now: number) => {
    const progress = Math.min(1, (now - start) / 450)
    lift.value = from + (target - from) * (1 - Math.pow(1 - progress, 3))
    if (progress < 1) armFrame = requestAnimationFrame(animate)
  }
  armFrame = requestAnimationFrame(animate)
})
type Person = typeof characters[number]
function handY(person: Person) { return person.faceY + Math.min(145, person.height - person.faceY - 52) * (1 - lift.value) }
function armPath(person: Person, right: boolean) {
  const shoulder = right ? person.width - 8 : 8
  const wrist = person.width / 2 + (right ? 29 : -29)
  const elbow = right ? person.width + 26 : -26
  const elbowY = person.faceY + 156 - lift.value * 76
  return `M${shoulder} ${person.faceY + 98} C${elbow} ${elbowY} ${wrist} ${handY(person) + 62} ${wrist} ${handY(person) + 32}`
}
let frame = 0
let media: MediaQueryList | undefined
function follow(event: PointerEvent) {
  if (media?.matches || event.pointerType === 'touch') return
  cancelAnimationFrame(frame)
  frame = requestAnimationFrame(() => {
    looks.value = characters.map((_, index) => {
      const box = scene.value?.querySelector(`[data-face="${index}"]`)?.getBoundingClientRect()
      if (!box) return { x: 0, y: 0 }
      const dx = event.clientX - box.left - box.width / 2, dy = event.clientY - box.top - box.height / 2
      const distance = Math.hypot(dx, dy) || 1
      return { x: dx / distance * Math.min(7, distance / 28), y: dy / distance * Math.min(6, distance / 35) }
    })
  })
}
function reset() { cancelAnimationFrame(frame); looks.value = characters.map(() => ({ x: 0, y: 0 })) }
onMounted(() => { media = window.matchMedia?.('(prefers-reduced-motion: reduce)'); window.addEventListener('pointermove', follow); document.documentElement.addEventListener('pointerleave', reset) })
onBeforeUnmount(() => { cancelAnimationFrame(armFrame); cancelAnimationFrame(frame); window.removeEventListener('pointermove', follow); document.documentElement.removeEventListener('pointerleave', reset) })
</script>
<template>
  <div
    class="companion-scene"
    :class="{ 'is-covering': covering }"
    data-test="companions"
    :data-covering="covering"
  >
    <div
      class="speech"
      aria-live="polite"
    >
      {{ covering ? t('login.artPrivacy') : t('login.artHello') }}<span />
    </div>
    <svg
      ref="scene"
      viewBox="0 0 760 540"
      role="img"
      :aria-label="t('login.artAlt')"
    >
      <ellipse
        cx="378"
        cy="483"
        rx="306"
        ry="25"
        fill="#C5D6CB"
        opacity=".45"
      />
      <path
        d="M37 167c-19-35 16-60 43-31m568-22c38-28 59 4 40 25M95 93l12-20m-33 20-7-16"
        fill="none"
        stroke="#6D9382"
        stroke-width="3"
        stroke-linecap="round"
      />
      <path
        d="m615 43 4 15 16 4-16 5-4 15-4-15-15-5 15-4Z"
        fill="#16756B"
      /><circle
        cx="196"
        cy="161"
        r="5"
        fill="#A5BCA9"
      />
      <g
        v-for="(person, i) in characters"
        :key="i"
        :transform="`translate(${person.x} ${person.y}) rotate(${person.angle} ${person.width / 2} ${person.height})`"
        class="person"
      >
        <path
          :d="`M38 ${person.height - 10}v34h-24m${person.width - 68} -34v34h24`"
          fill="none"
          stroke="#263D39"
          stroke-width="16"
          stroke-linecap="round"
        />
        <rect
          x="0"
          y="0"
          :width="person.width"
          :height="person.height"
          :rx="person.width / 2"
          :fill="person.color"
        />
        <path
          v-if="i === 0"
          d="M56 3q5-27 25-20m2 21q22-22 35-10"
          fill="none"
          stroke="#263D39"
          stroke-width="7"
          stroke-linecap="round"
        />
        <path
          v-if="i === 2"
          d="M-2 71Q24-7 83 2q61 4 84 72c-39 6-67-11-91-36-11 22-36 31-78 33Z"
          fill="#514768"
        />
        <g
          :data-face="i"
          :transform="`translate(${person.width / 2} ${person.faceY})`"
        >
          <ellipse
            cx="-28"
            cy="0"
            rx="19"
            ry="24"
            fill="#FFFDF5"
          /><ellipse
            cx="28"
            cy="0"
            rx="19"
            ry="24"
            fill="#FFFDF5"
          />
          <g
            class="pupils"
            :transform="`translate(${covering ? 0 : looks[i].x} ${covering ? 0 : looks[i].y})`"
          ><ellipse
            cx="-28"
            cy="1"
            rx="7"
            ry="10"
            fill="#233B35"
          /><ellipse
            cx="28"
            cy="1"
            rx="7"
            ry="10"
            fill="#233B35"
          /><circle
            cx="-26"
            cy="-2"
            r="2"
            fill="white"
          /><circle
            cx="30"
            cy="-2"
            r="2"
            fill="white"
          /></g>
          <path
            :d="covering ? 'M-8 41q8-5 16 0' : 'M-11 37q11 13 22 0'"
            fill="none"
            stroke="#263D39"
            stroke-width="4"
            stroke-linecap="round"
          />
          <ellipse
            v-if="i !== 1"
            cx="-52"
            cy="28"
            rx="12"
            ry="6"
            fill="#DC6D65"
            opacity=".45"
          /><ellipse
            v-if="i !== 1"
            cx="52"
            cy="28"
            rx="12"
            ry="6"
            fill="#DC6D65"
            opacity=".45"
          />
        </g>
        <path
          :d="armPath(person, false) + armPath(person, true)"
          fill="none"
          :stroke="person.shade"
          stroke-width="19"
          stroke-linecap="round"
        />
        <g
          class="hand hand-left"
          :transform="`translate(${person.width / 2 - 29} ${handY(person)})`"
        >
          <path
            d="M-14 23q-13-13-11-25l2-20q1-7 7-4l3 14v-20q3-8 8-1l2 19 3-22q5-6 8 2l-1 22 4-14q5-5 8 2l-3 25q4-12 10-8 5 4-4 18Q12 29 9 34Q0 40-9 34Q-12 29-14 23Z"
            fill="#F6D9A9"
            stroke="#D3AE7B"
            stroke-width=".8"
          />
        </g>
        <g
          class="hand hand-right"
          :transform="`translate(${person.width / 2 + 29} ${handY(person)})`"
        >
          <path
            d="M14 23q13-13 11-25l-2-20q-1-7-7-4l-3 14v-20q-3-8-8-1l-2 19-3-22q-5-6-8 2l1 22-4-14q-5-5-8 2l3 25q-4-12-10-8-5 4 4 18Q-12 29-9 34Q0 40 9 34Q12 29 14 23Z"
            fill="#F6D9A9"
            stroke="#D3AE7B"
            stroke-width=".8"
          />
        </g>
      </g>
      <path
        d="M233 486q145 18 293-1"
        stroke="#819D8D"
        stroke-width="2"
        fill="none"
        stroke-linecap="round"
      />
    </svg>
  </div>
</template>
<style scoped>
.companion-scene { position: relative; width: 100%; max-width: 850px; margin: auto; }.companion-scene svg { width: 100%; display: block; overflow: visible; }
.speech { position: absolute; top: 22%; left: 7%; z-index: 1; background: #fffdf5; color: #24443A; border: 1px solid #ADC3B4; padding: 12px 20px; border-radius: 22px 22px 2px 22px; font-size: 14px; transform: rotate(-7deg); transition: transform .3s; }.is-covering .speech { transform: rotate(-3deg); }
.pupils { transition: transform 90ms ease-out; }
@media(prefers-reduced-motion:reduce) { .hand, .pupils, .speech { transition: none; } }
@media(max-width:650px) { .speech { font-size: 11px; padding: 8px 12px; } }
</style>
