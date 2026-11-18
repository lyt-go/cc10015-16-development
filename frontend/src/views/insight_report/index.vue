<template>
  <section class="page" data-module="insight_report">
    <header class="page-head">
      <div><h2>洞察报告</h2><p class="page-desc">维护洞察报告的输入、执行约束与结果记录，为洞察报告提供可验证的数据工程流程。</p></div>
    </header>
    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span :class="m.trend > 0 ? 'trend-up' : 'trend-down'" style="font-size:12px">{{ m.trend > 0 ? '↑' : '↓' }}{{ Math.abs(m.trend) }}%</span>
      </article>
    </div>
    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label v-for="field in paramFields" :key="field"><span>{{ field }}</span>
        <select v-if="field.includes('方法')||field.includes('策略')||field.includes('粒度')" v-model="params[field]"><option v-for="o in options[field]" :key="o" :value="o">{{ o }}</option></select>
        <input v-else v-model="params[field]" :placeholder="`请输入${field}`" />
      </label>
      <button class="btn primary" type="submit" data-testid="run-analysis">运行分析</button>
    </form>
    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }"><span>{{ bar.label }}</span></div>
    </div>
    <table class="data-table" v-if="results.length">
      <thead><tr><th v-for="c in resultColumns" :key="c">{{ c }}</th></tr></thead>
      <tbody><tr v-for="row in results" :key="row.id"><td v-for="c in resultColumns" :key="c">{{ row[c] ?? '—' }}</td></tr></tbody>
    </table>
    <p v-if="!results.length && !loading" style="text-align:center;color:var(--muted)">请配置参数后运行分析</p>

    <!-- V016 跨模块联动：版本发布与导出文件清单核对 -->
    <section class="v016-panel" data-testid="v016-panel" style="margin-top:24px">
      <h3>洞察报告 V016：版本发布与导出文件清单核对</h3>
      <p class="page-desc">报告版本更新与同批次导出在同一事务内完成；发布后可按报告编号核对导出文件里的结论版本。</p>
      <div class="filter-bar">
        <label class="filter-item"><span>报告记录ID</span><input v-model="versionForm.entryId" type="number" min="1" placeholder="如 1" data-testid="version-entry-id" /></label>
        <label class="filter-item"><span>新版本号</span><input v-model="versionForm.newVersion" placeholder="如 v2.1" data-testid="version-new-value" /></label>
        <button class="btn primary" type="button" data-testid="publish-version" @click="publishVersion">发布新版本并联动导出批次</button>
      </div>
      <p v-if="versionMessage" :class="versionOk ? '' : 'error-text'" data-testid="version-message">{{ versionMessage }}</p>
      <pre v-if="versionDetail" class="json-box" data-testid="version-detail">{{ versionDetail }}</pre>

      <div class="filter-bar" style="margin-top:12px">
        <label class="filter-item"><span>报告编号</span><input v-model="crossCheckReportNo" placeholder="如 INSI-0003" data-testid="cross-check-report-no" /></label>
        <button class="btn primary" type="button" data-testid="run-cross-check" @click="runCrossCheck">核对导出文件清单</button>
      </div>
      <div v-if="crossCheck" data-testid="cross-check-result">
        <p>报告 {{ crossCheck.报告编号 }} 当前版本 <strong>{{ crossCheck.报告当前版本 }}</strong>，
          核对批次 {{ crossCheck.核对批次数量 }} 个，
          <span :class="crossCheck.全部通过 ? 'trend-up' : 'trend-down'">{{ crossCheck.全部通过 ? '全部通过' : '存在不一致' }}</span>
        </p>
        <table class="data-table" v-if="crossCheck.核对结果.length">
          <thead><tr><th>导出批次</th><th>批次状态</th><th>留档批次号</th><th>文件</th><th>结论版本</th><th>版本一致</th><th>指纹一致</th></tr></thead>
          <tbody>
            <template v-for="c in crossCheck.核对结果" :key="c.manifest_id">
              <tr v-for="f in c.文件核对" :key="f.file_id" :data-testid="`cross-check-file-${f.file_id}`">
                <td>{{ c.导出批次 }}</td><td>{{ c.批次状态 }}</td><td>{{ c.留档批次号 || '—' }}</td>
                <td style="max-width:260px;word-break:break-all">{{ f.文件名称 }}</td>
                <td>{{ f.文件内结论版本 }}</td>
                <td>{{ f.版本一致 ? '是' : '否' }}</td>
                <td>{{ f.指纹一致 ? '是' : '否' }}</td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </section>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/insight_report'
const summaryMetrics = ref<{label:string;value:number;trend:number}[]>([])
const results = ref<any[]>([]); const chartData = ref<{label:string;value:number}[]>([])
const loading = ref(false)
const params = ref<Record<string,string>>({})
const paramFields = ["报告编号", "分析主题", "数据来源", "关键发现", "建议措施", "影响范围", "生成时间", "报告版本"].filter((f: string) => !['编号','名称','版本'].includes(f)).slice(0, 4)
const options: Record<string,string[]> = { '统计粒度': ['按日','按周','按月'], '分组字段': ['全量','按类型','按区域'], '比较方法': ['环比','同比','绝对值'], '时间范围': ['最近7天','最近30天','最近90天'] }
const resultColumns = computed(() => ["报告编号", "分析主题", "数据来源", "关键发现", "建议措施", "影响范围", "生成时间", "报告版本"].slice(0, 5))

// ===== V016 跨模块联动 =====
const versionForm = reactive({ entryId: 1, newVersion: '' })
const versionMessage = ref(''); const versionOk = ref(true)
const versionDetail = ref('')
const crossCheckReportNo = ref('INSI-0003')
const crossCheck = ref<any>(null)

async function publishVersion() {
  versionMessage.value = ''; versionDetail.value = ''
  if (!versionForm.entryId || !versionForm.newVersion) {
    versionOk.value = false; versionMessage.value = '请填写报告记录ID与新版本号'; return
  }
  try {
    const res = await request(`${ENDPOINT}/${versionForm.entryId}/versions`, {
      method: 'POST',
      body: JSON.stringify({ values: { 报告版本: versionForm.newVersion } }),
    })
    const data = await res.json()
    if (!res.ok || data.ok === false) { versionOk.value = false; versionMessage.value = data.message ?? '发布失败'; return }
    versionOk.value = true
    versionMessage.value = data.message
    versionDetail.value = JSON.stringify(data.entry, null, 2)
  } catch (e: any) {
    versionOk.value = false; versionMessage.value = e?.message ?? '发布失败'
  }
}

async function runCrossCheck() {
  if (!crossCheckReportNo.value) return
  const res = await request(`${ENDPOINT}/v016/cross-check?report_no=${encodeURIComponent(crossCheckReportNo.value)}`)
  if (!res.ok) { versionOk.value = false; versionMessage.value = `核对失败：${res.status}`; return }
  crossCheck.value = await res.json()
}

function barHeight(v: number) { const max = Math.max(...chartData.value.map(d => d.value), 1); return Math.max(10, (v / max) * 140) }
async function fetchSummary() { try { const res = await request(`${ENDPOINT}/summary`); summaryMetrics.value = (await res.json()).metrics || [] } catch(e) {} }
async function runAnalysis() {
  loading.value = true
  try { const res = await request(`${ENDPOINT}/run`, { method: 'POST', body: JSON.stringify({ values: params.value }) }); const data = await res.json(); results.value = data.items || []; chartData.value = (data.items || []).map((r: any) => ({ label: Object.values(r)[0] as string, value: Object.values(r).filter(v => typeof v === 'number')[0] as number || 0 })).slice(0, 6) } catch(e) { results.value = [] }
  loading.value = false
}
onMounted(fetchSummary)
</script>
<style scoped>
.json-box { background: #0f172a; color: #d7e3ff; padding: 10px; border-radius: 6px; font-size: 12px; overflow-x: auto; white-space: pre-wrap; }
.v016-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
</style>
