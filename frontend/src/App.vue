<!-- 应用根组件：配置 Naive UI 主题、中文区域设置和全局反馈 Provider。 -->
<template>
  <NConfigProvider
    :theme="darkTheme"
    :theme-overrides="themeOverrides"
    :locale="zhCN"
    :date-locale="dateZhCN"
  >
    <NDialogProvider>
      <NNotificationProvider placement="bottom-right">
        <NMessageProvider>
          <RouterView />
        </NMessageProvider>
      </NNotificationProvider>
    </NDialogProvider>
  </NConfigProvider>
</template>

<script setup lang="ts">
import {
  darkTheme,
  NConfigProvider,
  NDialogProvider,
  NMessageProvider,
  NNotificationProvider,
  zhCN,
  dateZhCN,
} from 'naive-ui'
import type { GlobalThemeOverrides } from 'naive-ui'

const themeOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: '#c7ff4a',
    primaryColorHover: '#ddff84',
    primaryColorPressed: '#9fd332',
    primaryColorSuppl: '#c7ff4a',
    infoColor: '#5ea1ff',
    successColor: '#50d890',
    warningColor: '#f5b942',
    errorColor: '#ff637d',
    bodyColor: '#080b10',
    cardColor: '#0e131b',
    modalColor: '#111720',
    popoverColor: '#131a24',
    borderColor: '#242d39',
    dividerColor: '#202834',
    textColorBase: '#ecf2f8',
    textColor1: '#ecf2f8',
    textColor2: '#aab5c3',
    textColor3: '#778393',
    fontFamily: "Inter, 'IBM Plex Sans', 'Segoe UI', sans-serif",
    fontFamilyMono: "'JetBrains Mono', 'SFMono-Regular', Consolas, monospace",
    borderRadius: '10px',
    borderRadiusSmall: '7px',
  },
  Button: {
    fontWeight: '650',
  },
  Card: {
    borderColor: '#222b36',
  },
  DataTable: {
    thColor: '#0b1017',
    tdColor: '#0e131b',
    tdColorHover: '#141c26',
    borderColor: '#202833',
  },
}
</script>
