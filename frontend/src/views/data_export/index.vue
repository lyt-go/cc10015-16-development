<template>
  <section class="page" data-module="data_export">
    <header class="page-head">
      <div><h2>数据导出</h2><p class="page-desc">维护导出任务的输入、执行约束与结果记录；V016 起按字段清单生成导出文件清单，可回查报告版本与导出批次。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记导出任务</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出作业清单</button>
        <button class="btn primary" type="button" data-testid="open-manifest-create" @click="openManifestCreate">登记导出文件清单</button>
        <button class="btn" type="button" data-testid="reload-manifests" @click="reloadAll">刷新清单</button>
      </div>
    </header>
    <div class="filter-bar">
      <label><span>开始日期</span><input type="date" v-model="dateFrom" /></label>
      <label><span>结束日期</span><input type="date" v-model="dateTo" /></label>
      <button class="btn" type="button" @click="reload">查询</button>
    </div>
    <div class="timeline-wrap">
      <div v-for="item in timelineItems" :key="item.id" class="timeline-row">
        <span class="timeline-label">{{ item.name || item.label }}</span>
        <div class="timeline-track">
          <div class="timeline-bar" :class="`status-${item.status}`" :style="{ left: barLeft(item) + '%', width: barWidth(item) + '%' }" :data-testid="`bar-${item.id}`" :title="`${item.start} ~ ${item.end}`" @click="selectItem(item)">&nbsp;</div>
        </div>
        <span style="font-size:12px;color:var(--muted);margin-left:8px;">{{ item.start }} ~ {{ item.end }}</span>
      </div>
      <p v-if="!timelineItems.length" style="text-align:center;color:var(--muted)">暂无作业记录</p>
    </div>
    <div v-if="selectedItem" class="timeline-log" data-testid="timeline-log">
      <strong>{{ selectedItem.name || selectedItem.label }}</strong> — 状态：{{ selectedItem.status }}
      <div style="margin-top:4px;">{{ selectedItem.logs || '暂无日志' }}</div>
    </div>

    <!-- V016 导出文件清单：导航页表格，与抽屉共用 useExportManifests 的同一份数据 -->
    <div class="manifest-section" data-testid="manifest-section">
      <h3>导出文件清单（V016）</h3>
      <p v-if="manifestError" class="error-text" data-testid="manifest-error">{{ manifestError }}</p>
      <p v-if="loading" style="color:var(--muted)">清单加载中…</p>
      <table class="data-table" v-if="manifests.length">
        <thead>
          <tr>
            <th v-for="col in manifestColumns" :key="col">{{ col }}</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="m in manifests" :key="m.id" :data-testid="`manifest-row-${m.id}`" :data-status="m.状态">
            <td>{{ m.任务编号 }}</td>
            <td>{{ m.导出批次 }}</td>
            <td>{{ m.报告编号 }}</td>
            <td>{{ m.报告版本 }}</td>
            <td>{{ m.版本捕获方式 }}</td>
            <td>{{ m.后续新版本 || '—' }}</td>
            <td>{{ m.可下载文件数 }}/{{ m.文件数量 }}</td>
            <td>{{ m.缺失片段数 }}</td>
            <td>{{ m.未处理残留数 }}</td>
            <td><span :class="`status-tag status-${m.状态}`">{{ m.状态 }}</span></td>
            <td>{{ m.留档批次号 || '—' }}</td>
            <td>
              <button class="btn" type="button" data-testid="open-drawer" @click="openDrawer(m.id)">清单详情</button>
              <a class="btn" :href="`${ENDPOINT}/manifests/${m.id}/package`" data-testid="package-manifest">打包下载</a>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else-if="!loading" style="text-align:center;color:var(--muted)">暂无导出文件清单</p>
    </div>

    <!-- 详情抽屉：读取与导航页同一业务数据源 -->
    <div v-if="selectedId !== null" class="drawer-mask" data-testid="drawer-mask" @click.self="closeDrawer">
      <aside class="drawer" data-testid="manifest-drawer">
        <header class="drawer-head">
          <h3>导出批次 {{ selectedManifest?.导出批次 }}</h3>
          <button class="btn" type="button" @click="closeDrawer">关闭</button>
        </header>
        <p v-if="actionMessage" class="action-message" data-testid="action-message">{{ actionMessage }}</p>
        <div v-if="!detail" style="color:var(--muted)">详情加载中…</div>
        <div v-else class="drawer-body">
          <section class="drawer-block">
            <h4>清单记录（可回查报告版本与导出批次）</h4>
            <dl class="kv-list">
              <template v-for="col in manifestColumns" :key="col">
                <dt>{{ col }}</dt><dd>{{ (detail.manifest as any)[col] || '—' }}</dd>
              </template>
            </dl>
            <div class="drawer-actions">
              <button class="btn" type="button" data-testid="resume-plan" @click="showResumePlan(detail.manifest.id)">查看续写计划</button>
              <button class="btn" type="button" data-testid="archive-check" :disabled="detail.manifest.状态 !== '已完成'" @click="doArchiveCheck(detail.manifest.id)">V016留档核对</button>
              <button class="btn primary" type="button" data-testid="handoff" :disabled="!detail.manifest.留档批次号 || detail.manifest.状态 === '已交接'" @click="doHandoff(detail.manifest.id)">转交接收方V016</button>
            </div>
          </section>

          <section class="drawer-block">
            <h4>文件记录（下载 / 打包 / 书签登记）</h4>
            <table class="data-table">
              <thead><tr><th>文件名称</th><th>报告版本</th><th>状态</th><th>指纹(前12位)</th><th>书签</th><th>操作</th></tr></thead>
              <tbody>
                <tr v-for="f in detail.files" :key="f.id" :data-testid="`file-row-${f.id}`" :data-status="f.状态">
                  <td style="max-width:260px;word-break:break-all">{{ f.文件名称 }}</td>
                  <td>{{ f.报告版本 }}</td>
                  <td>{{ f.状态 }}</td>
                  <td>{{ (f.内容指纹 || '').slice(0, 12) || '—' }}</td>
                  <td>{{ f.书签登记 ? '已登记' : '未登记' }}</td>
                  <td>
                    <a v-if="f.状态 === '可下载'" class="btn" :href="f.下载路径" data-testid="download-file">下载</a>
                    <button class="btn" type="button" data-testid="write-segment" :disabled="f.状态 !== '续写中'" @click="doWriteSegment(f.id)">续写缺失片段</button>
                    <button class="btn" type="button" data-testid="bookmark" :disabled="f.状态 !== '可下载' || f.书签登记" @click="doBookmark(f.id)">书签登记</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </section>

          <section class="drawer-block">
            <h4>片段检查点（中断后只续写缺失片段）</h4>
            <table class="data-table">
              <thead><tr><th>文件</th><th>片段序号</th><th>范围</th><th>已写字节</th><th>状态</th></tr></thead>
              <tbody>
                <tr v-for="c in detail.checkpoints" :key="c.id" :data-testid="`checkpoint-${c.id}`">
                  <td>{{ c.file_id }}</td><td>{{ c.片段序号 }}</td><td>{{ c.片段范围 }}</td><td>{{ c.已写字节 }}</td><td>{{ c.状态 }}</td>
                </tr>
              </tbody>
            </table>
            <p v-if="resumePlanText" class="resume-plan" data-testid="resume-plan-text">{{ resumePlanText }}</p>
          </section>

          <section class="drawer-block">
            <h4>报告版本冲突判定</h4>
            <button class="btn" type="button" data-testid="check-conflict" @click="doCheckConflict(detail.manifest.id)">查看版本冲突</button>
            <button class="btn" type="button" data-testid="refresh-latest" :disabled="detail.manifest.版本捕获方式 !== '最新版本' || detail.manifest.状态 !== '导出中'" @click="doRefreshLatest(detail.manifest.id)">导出最新报告版本</button>
            <button class="btn" type="button" data-testid="add-followup" @click="doAddFollowup(detail.manifest.id)">登记后续新版本</button>
            <pre v-if="conflictText" class="json-box" data-testid="conflict-text">{{ conflictText }}</pre>
          </section>

          <section class="drawer-block">
            <h4>残留与整体回滚（按V016文件清单记录核对）</h4>
            <table class="data-table" v-if="detail.residuals.length">
              <thead><tr><th>残留说明</th><th>发现时间</th><th>状态</th></tr></thead>
              <tbody>
                <tr v-for="r in detail.residuals" :key="r.id"><td>{{ r.残留说明 }}</td><td>{{ r.发现时间 }}</td><td>{{ r.状态 }}</td></tr>
              </tbody>
            </table>
            <p v-else style="color:var(--muted)">无残留记录</p>
            <div class="drawer-actions">
              <button class="btn" type="button" data-testid="record-residual" @click="doRecordResidual(detail.manifest.id)">登记残留</button>
              <button class="btn danger" type="button" data-testid="rollback" :disabled="!detail.residuals.some((r: any) => r.状态 !== '已处理')" @click="doRollback(detail.manifest.id)">核对并整体回滚</button>
            </div>
          </section>

          <section class="drawer-block">
            <h4>V016留档核对与交接记录</h4>
            <table class="data-table" v-if="detail.audits.length">
              <thead><tr><th>核对项</th><th>结果</th><th>留档批次号</th><th>时间</th><th>详情</th></tr></thead>
              <tbody>
                <tr v-for="a in detail.audits" :key="a.id"><td>{{ a.核对项 }}</td><td>{{ a.核对结果 }}</td><td>{{ a.留档批次号 || '—' }}</td><td>{{ a.核对时间 }}</td><td style="max-width:300px">{{ a.详情 }}</td></tr>
              </tbody>
            </table>
            <table class="data-table" v-if="detail.handoffs.length">
              <thead><tr><th>接收方</th><th>交接时间</th><th>文件数</th><th>留档批次号</th><th>核对结果</th></tr></thead>
              <tbody>
                <tr v-for="h in detail.handoffs" :key="h.id"><td>{{ h.接收方 }}</td><td>{{ h.交接时间 }}</td><td>{{ h.清单文件数 }}</td><td>{{ h.留档批次号 }}</td><td>{{ h.核对结果 }}</td></tr>
              </tbody>
            </table>
          </section>
        </div>
      </aside>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记导出任务</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>

    <div v-if="showManifestCreate" class="modal-backdrop" data-testid="manifest-create-modal">
      <form class="modal" @submit.prevent="submitManifestCreate"><h3>登记导出文件清单（V016）</h3>
        <label class="create-field"><span>任务编号</span><input v-model="manifestForm.任务编号" placeholder="如 DATA-0001" required /></label>
        <label class="create-field"><span>报告编号</span><input v-model="manifestForm.报告编号" placeholder="如 INSI-0003" required /></label>
        <label class="create-field"><span>版本捕获方式</span>
          <select v-model="manifestForm.版本捕获方式" data-testid="capture-mode">
            <option value="最新版本">导出最新报告版本</option>
            <option value="冻结版本">导出任务冻结时捕获的报告版本</option>
          </select>
        </label>
        <label class="create-field"><span>文件数量</span><input v-model.number="manifestForm.文件数量" type="number" min="1" value="1" required /></label>
        <p v-if="actionMessage" class="error-text">{{ actionMessage }}</p>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showManifestCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-manifest-create">提交</button>
        </div>
      </form>
    </div>
  </section>
</template>
<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { request } from '@/api/client'
import { useExportManifests } from './useExportManifests'

const ENDPOINT = '/api/data_export'
const columns = ["任务编号", "数据来源", "导出格式", "字段清单", "数据范围", "压缩方式", "分片大小", "导出批次"]
const manifestColumns = ["任务编号", "导出批次", "报告编号", "报告版本", "版本捕获方式", "后续新版本", "可下载文件数", "缺失片段数", "未处理残留数", "状态", "留档批次号"]
const timelineItems = ref<any[]>([])
const selectedItem = ref<any>(null)
const dateFrom = ref(''); const dateTo = ref('')
const errorMessage = ref(''); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
const allItems = ref<any[]>([])

const {
  manifests, loading, errorMessage: manifestError, selectedId, selectedManifest, detail,
  loadManifests, openDetail, closeDetail, postAction,
} = useExportManifests()
const showManifestCreate = ref(false)
const actionMessage = ref('')
const resumePlanText = ref('')
const conflictText = ref('')
const manifestForm = reactive({ 任务编号: '', 报告编号: '', 版本捕获方式: '最新版本', 文件数量: 1 })

function barLeft(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 0; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return ((new Date(item.start).getTime() - min) / range) * 100 }
function barWidth(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 20; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return Math.max(5, ((new Date(item.end).getTime() - new Date(item.start).getTime()) / range) * 100) }
function selectItem(item: any) { selectedItem.value = selectedItem.value?.id === item.id ? null : item }
async function reload() {
  try { const res = await request(ENDPOINT); const data = await res.json(); allItems.value = data.items || []; applyFilter() } catch(e) { errorMessage.value = '加载失败' }
}
function applyFilter() {
  let items = allItems.value
  if(dateFrom.value) items = items.filter((i: any) => i.start >= dateFrom.value)
  if(dateTo.value) items = items.filter((i: any) => i.end <= dateTo.value)
  timelineItems.value = items; selectedItem.value = null
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }

function flash(message: string) {
  actionMessage.value = message
  window.setTimeout(() => { actionMessage.value = '' }, 4000)
}
async function reloadAll() {
  await reload()
  await loadManifests()
  if (selectedId.value !== null) await openDetail(selectedId.value)
}
function openManifestCreate() {
  manifestForm.任务编号 = ''; manifestForm.报告编号 = ''
  manifestForm.版本捕获方式 = '最新版本'; manifestForm.文件数量 = 1
  showManifestCreate.value = true
}
async function submitManifestCreate() {
  try {
    const data = await postAction(`${ENDPOINT}/manifests`, { values: { ...manifestForm } })
    showManifestCreate.value = false
    flash(data.message)
    await loadManifests()
  } catch (e: any) {
    flash(e.message)
  }
}
async function openDrawer(id: number) {
  resumePlanText.value = ''; conflictText.value = ''
  await openDetail(id)
}
function closeDrawer() {
  closeDetail()
}
async function doWriteSegment(fileId: number) {
  try {
    const data = await postAction(`${ENDPOINT}/files/${fileId}/segments`, { values: { 片段序号: 2, 总片段数: 2 } })
    flash(data.message)
    await reloadAll()
  } catch (e: any) { flash(e.message) }
}
async function doBookmark(fileId: number) {
  try { const data = await postAction(`${ENDPOINT}/files/${fileId}/bookmark`); flash(data.message); await reloadAll() } catch (e: any) { flash(e.message) }
}
async function showResumePlan(manifestId: number) {
  try {
    const res = await request(`${ENDPOINT}/manifests/${manifestId}/resume`)
    const data = await res.json()
    resumePlanText.value = JSON.stringify(data, null, 2)
  } catch (e: any) { flash('续写计划加载失败') }
}
async function doRecordResidual(manifestId: number) {
  const note = window.prompt('请输入残留说明', '分片写入失败')
  if (!note) return
  try { const data = await postAction(`${ENDPOINT}/manifests/${manifestId}/residuals`, { values: { 残留说明: note } }); flash(data.message); await reloadAll() } catch (e: any) { flash(e.message) }
}
async function doRollback(manifestId: number) {
  try { const data = await postAction(`${ENDPOINT}/manifests/${manifestId}/rollback`); flash(data.message); await reloadAll() } catch (e: any) { flash(e.message) }
}
async function doArchiveCheck(manifestId: number) {
  try { const data = await postAction(`${ENDPOINT}/manifests/${manifestId}/archive-check`); flash(data.message); await reloadAll() } catch (e: any) { flash(e.message) }
}
async function doHandoff(manifestId: number) {
  try { const data = await postAction(`${ENDPOINT}/manifests/${manifestId}/handoff`, { values: { 接收方: '接收方V016' } }); flash(data.message); await reloadAll() } catch (e: any) { flash(e.message) }
}
async function doCheckConflict(manifestId: number) {
  try {
    const res = await request(`${ENDPOINT}/manifests/${manifestId}/version-conflict`)
    conflictText.value = JSON.stringify(await res.json(), null, 2)
  } catch (e: any) { flash('版本冲突查询失败') }
}
async function doRefreshLatest(manifestId: number) {
  try { const data = await postAction(`${ENDPOINT}/manifests/${manifestId}/version-conflict/refresh`); flash(data.message); await reloadAll() } catch (e: any) { flash(e.message) }
}
async function doAddFollowup(manifestId: number) {
  const version = window.prompt('请输入冻结之后发布的后续新版本号', 'v2.1')
  if (!version) return
  try { const data = await postAction(`${ENDPOINT}/manifests/${manifestId}/version-conflict/followup`, { values: { 报告版本: version } }); flash(data.message); await reloadAll() } catch (e: any) { flash(e.message) }
}
onMounted(async () => {
  await reload()
  await loadManifests()
})
</script>
<style scoped>
.manifest-section { margin-top: 24px; }
.status-tag { padding: 2px 8px; border-radius: 10px; font-size: 12px; background: #eef2ff; color: #3b4cca; }
.status-tag.status-已回滚 { background: #fde8e8; color: #c0392b; }
.status-tag.status-已交接 { background: #e6f7ec; color: #1e8449; }
.drawer-mask { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); z-index: 100; display: flex; justify-content: flex-end; }
.drawer { width: 720px; max-width: 92vw; height: 100%; background: #fff; overflow-y: auto; padding: 20px 24px; box-shadow: -8px 0 24px rgba(0,0,0,0.12); }
.drawer-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.drawer-block { margin-bottom: 20px; }
.drawer-block h4 { margin: 0 0 8px; font-size: 14px; }
.drawer-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
.kv-list { display: grid; grid-template-columns: 130px 1fr; row-gap: 4px; font-size: 13px; margin: 0; }
.kv-list dt { color: var(--muted); }
.kv-list dd { margin: 0; word-break: break-all; }
.json-box, .resume-plan { background: #0f172a; color: #d7e3ff; padding: 10px; border-radius: 6px; font-size: 12px; overflow-x: auto; margin-top: 8px; white-space: pre-wrap; }
.action-message { background: #e6f7ec; color: #1e8449; padding: 8px 10px; border-radius: 6px; font-size: 13px; }
.error-text { color: #c0392b; }
.btn.danger { background: #c0392b; color: #fff; }
.btn[disabled] { opacity: 0.5; cursor: not-allowed; }
</style>
