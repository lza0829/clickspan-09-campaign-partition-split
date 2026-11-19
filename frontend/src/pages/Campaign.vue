<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const edges = ref([])
const audit = ref(null)
const err = ref('')

function fmt(code) {
  return code === null || code === undefined || code === '' ? '∅（空）' : code
}

onMounted(async () => {
  try {
    const [e, a] = await Promise.all([api.campaignEdges(), api.campaignAudit()])
    edges.value = e
    audit.value = a
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>

<template>
  <section class="panel">
    <h1>活动分区核证</h1>
    <p class="muted">
      只读视图：活动码变更（空变有 / 有变空 / A 变 B）强制收口旧段并另起新段；
      分区边记录旧段/新段与前后活动码，核证比对会话绑定码与成员击点 campaign_id 集合。
    </p>
    <p v-if="err" class="err">{{ err }}</p>

    <h2>核证结论</h2>
    <p :class="audit?.ok ? 'ok' : 'err'">
      <strong>{{ audit?.ok ? '全部一致' : '存在不一致' }}</strong>
      <span class="muted">（共 {{ audit?.sessions?.length || 0 }} 段）</span>
    </p>
    <table class="data">
      <thead>
        <tr><th>会话</th><th>访客</th><th>状态</th><th>绑定活动码</th><th>成员活动码集合</th><th>成员点数</th><th>结论</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in audit?.sessions || []" :key="r.session_id">
          <td class="mono">{{ r.session_id }}</td>
          <td>{{ r.visitor_id }}</td>
          <td>{{ r.status }}</td>
          <td class="mono">{{ fmt(r.bound_campaign_code) }}</td>
          <td class="mono">
            {{ r.member_has_null_code ? '∅ ' : '' }}{{ r.member_campaign_codes.join(', ') || (r.member_has_null_code ? '' : '∅') }}
          </td>
          <td>{{ r.member_hit_count }} / {{ r.declared_hit_count }}</td>
          <td :class="r.consistent ? 'ok' : 'err'">{{ r.consistent ? '一致' : `不符：${r.mismatch}` }}</td>
        </tr>
      </tbody>
    </table>

    <h2 style="margin-top:1.5rem">分区边</h2>
    <table class="data">
      <thead>
        <tr><th>#</th><th>访客</th><th>旧段</th><th>新段</th><th>前活动码</th><th>后活动码</th><th>触发击点</th><th>切分时刻</th><th>原因</th></tr>
      </thead>
      <tbody>
        <tr v-for="e in edges" :key="e.id">
          <td>{{ e.id }}</td>
          <td>{{ e.visitor_id }}</td>
          <td class="mono">{{ e.old_session_id || '—' }}</td>
          <td class="mono">{{ e.new_session_id }}</td>
          <td class="mono">{{ fmt(e.prev_campaign_code) }}</td>
          <td class="mono">{{ fmt(e.next_campaign_code) }}</td>
          <td class="mono">{{ e.split_hit_id || '—' }}</td>
          <td class="mono">{{ e.split_event_ts }}</td>
          <td>{{ e.reason }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
