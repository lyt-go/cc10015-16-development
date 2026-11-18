/**
 * V016 导出文件清单共享业务数据。
 *
 * 导航页（文件清单表格）与详情抽屉都只通过本 composable 拿数据，
 * 保证两个视图读取的是同一份业务数据，不会各发各的请求、各算各的状态。
 */
import { computed, ref } from 'vue'

import { request } from '@/api/client'

const ENDPOINT = '/api/data_export'

export type ManifestListItem = {
  id: number
  任务编号: string
  导出批次: string
  报告编号: string
  报告版本: string
  版本捕获方式: string
  后续新版本: string
  版本生成时间: string
  文件数量: number
  状态: string
  创建时间: string
  完成时间: string
  接收方: string
  留档批次号: string
  备注: string
  可下载文件数: number
  续写中文件数: number
  缺失片段数: number
  未处理残留数: number
}

export type ManifestDetail = {
  manifest: ManifestListItem
  files: Array<Record<string, any>>
  checkpoints: Array<Record<string, any>>
  audits: Array<Record<string, any>>
  handoffs: Array<Record<string, any>>
  residuals: Array<Record<string, any>>
}

const manifests = ref<ManifestListItem[]>([])
const loading = ref(false)
const errorMessage = ref('')
const selectedId = ref<number | null>(null)
const detail = ref<ManifestDetail | null>(null)
// 模块级单例：页面与抽屉共享同一份加载状态与数据
let loadSeq = 0

export function useExportManifests() {
  const selectedManifest = computed(
    () => manifests.value.find(m => m.id === selectedId.value) ?? null,
  )

  async function loadManifests(taskNo?: string) {
    const seq = ++loadSeq
    loading.value = true
    errorMessage.value = ''
    try {
      const query = taskNo ? `?task_no=${encodeURIComponent(taskNo)}` : ''
      const res = await request(`${ENDPOINT}/manifests${query}`)
      if (!res.ok) throw new Error(`接口返回 ${res.status}`)
      const data = await res.json()
      if (seq !== loadSeq) return
      manifests.value = data.items ?? []
    } catch (e: any) {
      if (seq === loadSeq) errorMessage.value = e?.message ?? '导出文件清单加载失败'
    } finally {
      if (seq === loadSeq) loading.value = false
    }
  }

  async function openDetail(id: number) {
    selectedId.value = id
    detail.value = null
    const res = await request(`${ENDPOINT}/manifests/${id}`)
    if (!res.ok) {
      errorMessage.value = `清单详情加载失败：${res.status}`
      return
    }
    detail.value = await res.json()
  }

  function closeDetail() {
    selectedId.value = null
    detail.value = null
  }

  async function postAction(path: string, body?: Record<string, any>) {
    const res = await request(path, {
      method: 'POST',
      body: JSON.stringify(body ?? {}),
    })
    const data = await res.json()
    if (!res.ok || data.ok === false) {
      throw new Error(data.message ?? '操作失败')
    }
    return data
  }

  return {
    manifests,
    loading,
    errorMessage,
    selectedId,
    selectedManifest,
    detail,
    loadManifests,
    openDetail,
    closeDetail,
    postAction,
  }
}
