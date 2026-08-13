/* 验证项目配置解析、合并和仓库路径安全规则。 */

import { describe, expect, it } from 'vitest'

import {
  isSafeRepositoryPath,
  mergePipelineConfig,
  parseKeyValue,
  validServiceName,
} from './projectConfig'

describe('project configuration helpers', () => {
  it('preserves environment values exactly after the first equals sign', () => {
    expect(parseKeyValue('TOKEN=  keep = spaces  \n# ignored\nEMPTY=')).toEqual({
      TOKEN: '  keep = spaces  ',
      EMPTY: '',
    })
  })

  it('rejects duplicate and invalid environment names', () => {
    expect(() => parseKeyValue('A=1\nA=2')).toThrow('环境变量 A 重复')
    expect(() => parseKeyValue('BAD-NAME=value')).toThrow('环境变量名 BAD-NAME 无效')
  })

  it('merges managed pipeline fields without dropping advanced keys', () => {
    expect(mergePipelineConfig({
      min_free_bytes: 1024,
      service_name: 'stale',
      default_environment_id: 'stale-env',
    }, {
      serviceName: 'api',
      defaultEnvironmentId: null,
    })).toEqual({ min_free_bytes: 1024, service_name: 'api' })
  })

  it('validates compose service names and repository-local paths', () => {
    expect(validServiceName('api.v2_worker')).toBe(true)
    expect(validServiceName('-api')).toBe(false)
    expect(isSafeRepositoryPath('deploy/compose.yaml')).toBe(true)
    expect(isSafeRepositoryPath('../compose.yaml')).toBe(false)
  })
})
