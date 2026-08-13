<!-- 单管理员登录页：提交凭据并建立控制台访问会话。 -->
<template>
  <main class="login-page">
    <section class="login-context">
      <div class="context-glow" />
      <div class="context-content">
        <div class="brand-lockup">
          <span class="brand-mark"><Boxes :size="22" /></span>
          <div><strong>FORGEDECK</strong><small>DEVOPS CONTROL PLANE</small></div>
        </div>
        <div class="hero-copy">
          <p class="eyebrow">BUILD · SHIP · OBSERVE</p>
          <h1>把交付链路<br /><em>收回自己手里。</em></h1>
          <p>从 Git 提交到可验证的容器部署，一套为轻量服务器设计的自托管控制平面。</p>
        </div>
        <div class="trust-grid">
          <div>
            <ShieldCheck :size="18" /><span><b>本地控制</b><small>凭据加密存储</small></span>
          </div>
          <div>
            <KeyRound :size="18" /><span><b>最小权限</b><small>操作全程审计</small></span>
          </div>
        </div>
      </div>
      <div class="build-readout mono">CONTROL PLANE / 01<br />RUNNER STATUS / STANDBY</div>
    </section>

    <section class="login-form-wrap">
      <div class="login-form-card">
        <div class="form-heading">
          <span class="auth-icon"><LockKeyhole :size="19" /></span>
          <div>
            <p class="eyebrow">SECURE ACCESS</p>
            <h2>管理员登录</h2>
          </div>
        </div>
        <p class="form-description">
          登录后可管理构建、部署和基础设施。所有敏感操作都会进入审计日志。
        </p>
        <NForm ref="formRef" :model="form" :rules="rules" size="large" @submit.prevent="submit">
          <NFormItem label="账号" path="username">
            <NInput
              v-model:value="form.username"
              placeholder="admin"
              autocomplete="username"
              @keyup.enter="submit"
            >
              <template #prefix><NIcon :component="UserRound" /></template>
            </NInput>
          </NFormItem>
          <NFormItem label="密码" path="password">
            <NInput
              v-model:value="form.password"
              type="password"
              show-password-on="mousedown"
              placeholder="输入管理员密码"
              autocomplete="current-password"
              @keyup.enter="submit"
            >
              <template #prefix><NIcon :component="LockKeyhole" /></template>
            </NInput>
          </NFormItem>
          <div class="form-options">
            <NCheckbox v-model:checked="remember">在此设备保持登录</NCheckbox>
          </div>
          <NButton type="primary" attr-type="submit" block size="large" :loading="session.loading">
            进入控制台<template #icon><NIcon :component="ArrowRight" /></template>
          </NButton>
        </NForm>
        <p class="security-note">
          <ShieldCheck :size="13" />API 使用 Bearer Token，会话过期后自动退出。
        </p>
      </div>
    </section>
  </main>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NButton,
  NCheckbox,
  NForm,
  NFormItem,
  NIcon,
  NInput,
  useMessage,
  type FormInst,
  type FormRules,
} from 'naive-ui'
import { ArrowRight, Boxes, KeyRound, LockKeyhole, ShieldCheck, UserRound } from 'lucide-vue-next'
import { ApiError } from '@/api/client'
import { useSessionStore } from '@/stores/session'

const router = useRouter()
const route = useRoute()
const session = useSessionStore()
const message = useMessage()
const formRef = ref<FormInst | null>(null)
const remember = ref(false)
const form = reactive({ username: '', password: '' })
const rules: FormRules = {
  username: { required: true, message: '请输入管理员账号', trigger: ['blur', 'input'] },
  password: { required: true, min: 6, message: '密码至少 6 个字符', trigger: ['blur', 'input'] },
}

async function submit(): Promise<void> {
  /** 提交登录表单并在成功后跳转用户原目标页面。 */
  try {
    await formRef.value?.validate()
    await session.login(form, remember.value)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    await router.replace(redirect)
  } catch (error) {
    if (error instanceof ApiError) message.error(error.message)
  }
}
</script>

<style scoped>
.login-page {
  display: grid;
  min-height: 100vh;
  grid-template-columns: minmax(0, 1.2fr) minmax(420px, 0.8fr);
}
.login-context {
  position: relative;
  overflow: hidden;
  padding: 48px clamp(36px, 6vw, 96px);
  border-right: 1px solid #1f2833;
  background: linear-gradient(145deg, #0b1017 0%, #0d141c 52%, #080c12 100%);
}
.login-context::before {
  position: absolute;
  inset: 0;
  opacity: 0.22;
  background-image:
    linear-gradient(rgba(255, 255, 255, 0.045) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255, 255, 255, 0.045) 1px, transparent 1px);
  background-size: 48px 48px;
  content: '';
  mask-image: linear-gradient(to bottom, black, transparent 82%);
}
.context-glow {
  position: absolute;
  top: 22%;
  left: 40%;
  width: 380px;
  height: 380px;
  border-radius: 50%;
  background: rgba(199, 255, 74, 0.08);
  filter: blur(80px);
}
.context-content {
  position: relative;
  z-index: 1;
  display: flex;
  min-height: calc(100vh - 96px);
  flex-direction: column;
}
.brand-lockup {
  display: flex;
  align-items: center;
  gap: 12px;
}
.brand-mark {
  display: grid;
  width: 42px;
  height: 42px;
  border: 1px solid rgba(199, 255, 74, 0.3);
  border-radius: 11px;
  color: #c7ff4a;
  background: rgba(199, 255, 74, 0.08);
  place-items: center;
}
.brand-lockup div {
  display: flex;
  flex-direction: column;
}
.brand-lockup strong {
  font-size: 15px;
  letter-spacing: 0.1em;
}
.brand-lockup small {
  margin-top: 2px;
  color: #5f6d7c;
  font-family: 'JetBrains Mono', monospace;
  font-size: 8px;
  letter-spacing: 0.18em;
}
.hero-copy {
  margin: auto 0;
}
.hero-copy h1 {
  margin: 0;
  font-size: clamp(46px, 5.6vw, 84px);
  font-weight: 680;
  letter-spacing: -0.065em;
  line-height: 0.98;
}
.hero-copy em {
  color: #c7ff4a;
  font-style: normal;
}
.hero-copy > p:last-child {
  max-width: 540px;
  margin: 28px 0 0;
  color: #8794a3;
  font-size: 15px;
  line-height: 1.8;
}
.trust-grid {
  display: grid;
  max-width: 520px;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.trust-grid > div {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 13px;
  border: 1px solid #202a35;
  border-radius: 11px;
  color: #8190a0;
  background: rgba(12, 18, 25, 0.7);
}
.trust-grid span {
  display: flex;
  flex-direction: column;
}
.trust-grid b {
  color: #bdc8d3;
  font-size: 11px;
}
.trust-grid small {
  margin-top: 2px;
  font-size: 10px;
}
.build-readout {
  position: absolute;
  right: 28px;
  bottom: 25px;
  color: #3f4a56;
  font-size: 8px;
  line-height: 1.8;
  letter-spacing: 0.12em;
  text-align: right;
}
.login-form-wrap {
  display: grid;
  padding: 36px;
  place-items: center;
  background: rgba(8, 11, 16, 0.9);
}
.login-form-card {
  width: min(100%, 420px);
}
.form-heading {
  display: flex;
  align-items: center;
  gap: 13px;
}
.auth-icon {
  display: grid;
  width: 42px;
  height: 42px;
  border: 1px solid #27313e;
  border-radius: 11px;
  color: #aeb9c7;
  background: #111821;
  place-items: center;
}
.form-heading .eyebrow {
  margin-bottom: 3px;
}
h2 {
  margin: 0;
  font-size: 28px;
  letter-spacing: -0.04em;
}
.form-description {
  margin: 18px 0 28px;
  color: #778493;
  font-size: 13px;
  line-height: 1.7;
}
.form-options {
  display: flex;
  justify-content: space-between;
  margin: -2px 0 22px;
}
.security-note {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  margin: 20px 0 0;
  color: #596675;
  font-size: 10px;
}
@media (max-width: 850px) {
  .login-page {
    grid-template-columns: 1fr;
  }
  .login-context {
    display: none;
  }
  .login-form-wrap {
    min-height: 100vh;
    padding: 24px;
  }
}
</style>
