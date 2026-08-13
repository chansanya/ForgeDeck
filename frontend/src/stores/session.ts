/* 管理管理员会话、访问 Token 和应用启动时的身份恢复。 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { api, tokenStorage } from '@/api/client'
import type { LoginRequest, User } from '@/api/types'

export const useSessionStore = defineStore('session', () => {
  const user = ref<User | null>(null)
  const initialized = ref(false)
  const loading = ref(false)
  const token = ref(tokenStorage.get())
  const isAuthenticated = computed(() => Boolean(token.value))

  async function hydrate(): Promise<void> {
    /** 应用启动时验证已有 Token，失效则清理本地会话。 */
    if (initialized.value) return
    initialized.value = true
    if (!token.value) return
    try {
      user.value = await api.auth.me()
    } catch {
      logout()
    }
  }

  async function login(credentials: LoginRequest, remember = false): Promise<void> {
    /** 登录并按用户选择将访问令牌保存到会话或本地存储。 */
    loading.value = true
    try {
      const result = await api.auth.login(credentials)
      tokenStorage.set(result.access_token, remember)
      token.value = result.access_token
      user.value = result.user || await api.auth.me()
      initialized.value = true
    } finally {
      loading.value = false
    }
  }

  function logout(): void {
    /** 清理令牌和用户状态，供退出按钮及 401 事件复用。 */
    tokenStorage.clear()
    token.value = null
    user.value = null
    initialized.value = true
  }

  return { user, initialized, loading, token, isAuthenticated, hydrate, login, logout }
})
