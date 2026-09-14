import { defineStore } from 'pinia'
import { ApiError, api, type User } from '@/api/client'

export const useAuthStore = defineStore('auth', {
  state: () => ({ user: null as User | null, loaded: false }),
  actions: {
    async fetchMe() {
      try {
        this.user = await api.me()
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) this.user = null
        else throw e
      } finally {
        this.loaded = true
      }
    },
    async loginBootstrap(username: string, password: string) {
      const { user } = await api.bootstrapLogin(username, password)
      this.user = user
      this.loaded = true
    },
    async logout() {
      try {
        await api.logout()
      } finally {
        this.user = null
      }
    },
  },
})
