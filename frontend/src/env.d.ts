/// <reference types="vite/client" />

/* 声明 Light DevOps 使用的 Vite 环境变量类型。 */

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
