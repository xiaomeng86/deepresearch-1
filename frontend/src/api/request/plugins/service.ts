/**
 * Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
 * 未经授权，禁止转售或仿制。
 */

import { AxiosResponse } from 'axios'
import { ResponseError } from '../error'
import { IRequestPlugin } from './plugin'

export const CODE_KEY = 'status'
export const MESSAGE_KEY = 'message'

/**
 * 接口级状态取值白名单
 *
 * 修复：早期实现把响应体顶层的 `status` 一律当作接口级状态码，只要不等于 'success'
 * 就判定为失败。但很多业务响应（如附件、文档）的 `status` 表示业务处理状态
 * （pending / processing / completed / failed），于是接口明明成功（HTTP 2xx），
 * 前端却抛出 'API data exception'。
 *
 * 现在只有当 `status` 属于接口级词表时才参与成功判定，业务状态一律放过。
 */
const API_STATUS_VALUES = ['success', 'ok', 'error', 'fail']

function isApiLevelStatus(value: unknown) {
  return typeof value === 'string' && API_STATUS_VALUES.includes(value.toLowerCase())
}

/**
 * 处理与后端接口上的约定
 */
export const servicePlugin: IRequestPlugin = {
  install(instance) {
    instance.interceptors.response.use(
      (response) => {
        const data = response?.data
        if (!response || !isObject(data)) return response
        if (!(CODE_KEY in data)) return response
        if (!isApiLevelStatus(data[CODE_KEY])) return response

        const code = data[CODE_KEY]
        if (code !== 'success' && code !== 'ok') {
          const message =
            data[MESSAGE_KEY] || data.detail || 'API data exception'
          const error = new ResponseError(message, response)
          return Promise.reject(error)
        }

        return response
      },
      (error) => {
        const response = error.response as AxiosResponse<any> | undefined

        const data = response?.data
        if (!response || !isObject(data)) return Promise.reject(error)
        if (!(CODE_KEY in data)) return Promise.reject(error)
        if (!isApiLevelStatus(data[CODE_KEY])) return Promise.reject(error)

        const code = data[CODE_KEY]
        if (code !== 'success' && code !== 'ok') {
          const message =
            data[MESSAGE_KEY] || data.detail || 'API data exception'
          const error = new ResponseError(message, response)
          return Promise.reject(error)
        }

        return Promise.reject(error)
      },
    )
  },
}

function isObject(value: unknown) {
  const type = typeof value
  return value != null && (type === 'object' || type === 'function')
}
