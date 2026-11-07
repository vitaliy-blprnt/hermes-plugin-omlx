import {
  Badge, Button, ConfirmDialog, EmptyState, ErrorState, GlyphSpinner, Input,
  gatewayActivationEpoch, host, useGatewayActivationEpoch,
  useMutation, useQuery, useQueryClient, useValue
} from '@hermes/plugin-sdk'
import { useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const secondary = 'text-xs text-(--ui-text-secondary)'
const row = 'flex flex-wrap items-center justify-between gap-3 py-3'

function errorText(error) {
  const message = typeof error === 'string' ? error : error?.message || 'The operation could not be completed. Try again.'
  if (error?.status === 404 || error?.statusCode === 404 || /\b404\b|plugin.*disabled/i.test(message)) {
    return 'Enable the Python plugin on this host and profile with hermes plugins enable omlx, then enable oMLX in Desktop → Capabilities → Plugins and refresh.'
  }
  return message.split('\n')[0].slice(0, 500)
}

function bytes(value) {
  if (!Number.isFinite(value) || value < 0) return '—'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  const index = value ? Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1) : 0
  return `${(value / 1024 ** index).toLocaleString(undefined, { maximumFractionDigits: index ? 1 : 0 })} ${units[index]}`
}

function Job({ job, cancel, cancelling }) {
  const running = job.status === 'running'
  const stopping = running && (job.cancel_requested || cancelling)
  const percent = job.total_bytes > 0 && Number.isFinite(job.downloaded_bytes)
    ? Math.min(100, Math.max(0, job.downloaded_bytes / job.total_bytes * 100)) : null
  const label = stopping ? 'Cancelling…' : job.status === 'cancelled' ? 'Cancelled'
    : job.status === 'complete' ? 'Complete' : job.status === 'error' ? 'Failed' : job.phase || 'Running'
  return jsxs('div', {
    className: 'grid gap-2 border-t border-(--ui-stroke-secondary) py-3',
    children: [
      jsxs('div', { className: 'flex flex-wrap items-center justify-between gap-2', children: [
        jsxs('div', { className: 'flex min-w-0 items-center gap-2', children: [
          running && jsx(GlyphSpinner, { ariaLabel: label }),
          jsx('span', { className: 'text-sm font-medium', children: job.kind }),
          jsx(Badge, { variant: job.status === 'error' ? 'destructive' : 'muted', children: label })
        ] }),
        running && job.kind === 'download' && job.can_cancel && jsx(Button, {
          variant: 'secondary', size: 'sm', disabled: stopping, onClick: () => cancel(job.id),
          children: stopping ? 'Cancelling…' : 'Cancel'
        })
      ] }),
      job.kind === 'download' && jsxs('div', { className: 'grid gap-1', children: [
        percent !== null && jsxs('div', { className: 'flex items-center gap-3', children: [
          jsx('progress', { className: 'h-1.5 min-w-0 flex-1 accent-(--ui-accent)', max: 100, value: percent, 'aria-label': 'Download completion' }),
          jsx('span', { className: `${secondary} tabular-nums`, children: `${percent.toFixed(1)}%` })
        ] }),
        jsx('p', { className: `${secondary} tabular-nums`, children: [
          `${bytes(job.cached_bytes)} already downloaded`,
          `${bytes(job.received_bytes)} received`,
          job.total_bytes > 0 ? `${bytes(job.total_bytes)} total` : null,
          running && !stopping && job.bytes_per_second > 0 ? `${bytes(job.bytes_per_second)}/s` : null
        ].filter(Boolean).join(' · ') })
      ] }),
      job.message && jsx('p', { className: `${secondary} break-words`, children: job.message }),
      job.status === 'error' && jsx('p', { role: 'alert', className: 'text-xs text-destructive break-words', children: errorText(job.error || 'The operation failed. Try again.') })
    ]
  })
}

function ScopedSettings({ ctx, connectionId, profile, epoch }) {
  const client = useQueryClient()
  const queryKey = ['omlx', 'status', connectionId, profile, epoch]
  const [repo, setRepo] = useState('')
  const [deleting, setDeleting] = useState(null)
  const inFlight = useRef(false)
  // ctx.rest captures ambient routing synchronously; refuse any late call after a route change.
  const isCurrent = () => gatewayActivationEpoch() === epoch
    && host.state.connectionId.get() === connectionId && host.state.profile.get() === profile
  const request = async (path, options) => {
    if (!isCurrent()) throw new Error('Host or profile changed. Refresh this page before continuing.')
    const result = await ctx.rest(path, options)
    if (!isCurrent()) throw new Error('Host or profile changed. Refresh this page before continuing.')
    return result
  }
  const status = useQuery({
    queryKey,
    queryFn: () => request('/status'),
    retry: false,
    refetchOnMount: 'always',
    refetchOnWindowFocus: true,
    refetchInterval: query => isCurrent() && query.state.data?.jobs?.some(job => job.status === 'running') ? 2500 : false,
    refetchIntervalInBackground: false
  })
  const action = useMutation({
    mutationFn: async ({ path, body }) => {
      if (inFlight.current) throw new Error('Wait for the current request to finish.')
      inFlight.current = true
      try {
        await client.cancelQueries({ queryKey, exact: true })
        const result = await request(path, { method: 'POST', body: body || {} })
        if (result?.error && !result.id) throw new Error(errorText(result.error))
        return result
      } finally {
        inFlight.current = false
      }
    },
    onSuccess: result => {
      if (!isCurrent()) return
      if (result?.id) client.setQueryData(queryKey, previous => previous && ({
        ...previous, jobs: [...(previous.jobs || []).filter(job => job.id !== result.id), result]
      }))
      void client.invalidateQueries({ queryKey, exact: true })
    }
  })
  const run = (path, body) => action.mutate({ path, body })
  const data = status.data
  const jobs = data?.jobs || []
  const running = jobs.filter(job => job.status === 'running')
  const busy = action.isPending || running.length > 0
  // Downloading bytes does not lock the runtime's on/off controls.
  const runtimeBusy = action.isPending || running.some(job => job.kind !== 'download')
  const unavailable = !!status.error || !isCurrent()
  const validRepo = /^[A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9][A-Za-z0-9._-]*$/.test(repo.trim())

  return jsxs('div', { className: 'mx-auto grid w-full max-w-2xl gap-6 p-4 text-(--ui-text-primary)', children: [
    jsxs('header', { children: [
      jsx('h2', { className: 'text-lg font-semibold', children: 'oMLX' }),
      jsx('p', { className: secondary, children: 'Managed inference on Apple Silicon' })
    ] }),
    status.isPending && jsxs('div', { className: 'flex items-center gap-2 text-sm', children: [
      jsx(GlyphSpinner, { ariaLabel: 'Loading oMLX' }), 'Loading oMLX…'
    ] }),
    status.error && jsx(ErrorState, {
      title: 'oMLX is unavailable', description: errorText(status.error),
      children: jsx(Button, { variant: 'secondary', onClick: () => status.refetch(), disabled: status.isFetching, children: 'Refresh' })
    }),
    action.error && jsx('div', { role: 'alert', className: 'text-sm text-destructive break-words', children: errorText(action.error) }),
    data && !unavailable && jsxs('div', { className: 'grid gap-6', children: [
      data.error && jsx('p', { role: 'alert', className: 'text-sm text-destructive break-words', children: errorText(data.error) }),
      data.supported === false ? jsx(EmptyState, {
        title: 'This host does not support oMLX', description: 'Connect to a supported Apple Silicon Mac to manage oMLX.'
      }) : jsxs('section', { 'aria-label': 'oMLX runtime', children: [
        jsxs('div', { className: row, children: [
          jsxs('div', { className: 'grid gap-1', children: [
            jsxs('div', { className: 'flex flex-wrap items-center gap-2', children: [
              jsx('h3', { className: 'text-sm font-medium', children: 'Runtime' }),
              jsx(Badge, { variant: 'muted', children: data.running ? 'Running' : data.installed ? 'Off' : 'Not installed' }),
              data.version && jsx('span', { className: secondary, children: `v${data.version}` })
            ] }),
            jsx('p', { className: secondary, children: `${profile} · ${connectionId || 'Local host'}` })
          ] }),
          !data.installed ? jsx(Button, { disabled: runtimeBusy || data.supported !== true, onClick: () => run('/install'), children: 'Install' })
            : jsx(Button, {
              variant: data.running ? 'secondary' : 'default', disabled: runtimeBusy || data.supported !== true,
              onClick: () => run(data.running ? '/stop' : '/start'), children: data.running ? 'Turn off' : 'Turn on'
            })
        ] })
      ] }),
      data.supported === true && jsxs('section', { className: 'grid gap-3', 'aria-labelledby': 'omlx-download-heading', children: [
        jsx('h3', { id: 'omlx-download-heading', className: 'text-sm font-medium', children: 'Download a model' }),
        jsxs('form', { className: 'flex flex-wrap items-end gap-2', onSubmit: event => { event.preventDefault(); if (validRepo && !busy) run('/download', { repo: repo.trim() }) }, children: [
          jsxs('label', { className: 'grid min-w-0 flex-1 gap-1 text-xs', children: [
            'Hugging Face owner/model', jsx(Input, {
              value: repo, onChange: event => setRepo(event.target.value), placeholder: 'owner/model',
              disabled: busy || !data.installed, autoComplete: 'off', spellCheck: false, required: true,
              'aria-describedby': 'omlx-download-hint'
            })
          ] }),
          jsx(Button, { type: 'submit', disabled: !validRepo || busy || !data.installed, children: 'Download' })
        ] }),
        jsx('p', { id: 'omlx-download-hint', className: secondary, children: data.installed ? 'Use an MLX-compatible model repository.' : 'Install the runtime before downloading models.' })
      ] }),
      jobs.length > 0 && jsx('section', { 'aria-label': 'Operations', children: jobs.map(job => jsx(Job, {
        job, cancelling: action.isPending && action.variables?.path === '/cancel' && action.variables?.body?.job_id === job.id,
        cancel: id => run('/cancel', { job_id: id })
      }, job.id)) }),
      data.supported === true && jsxs('section', { 'aria-labelledby': 'omlx-library-heading', children: [
        jsxs('div', { className: 'flex items-center justify-between gap-2', children: [
          jsx('h3', { id: 'omlx-library-heading', className: 'text-sm font-medium', children: 'Model library' }),
          jsx(Button, { variant: 'text', size: 'sm', disabled: status.isFetching || action.isPending, onClick: () => status.refetch(), children: 'Refresh' })
        ] }),
        !data.models?.length ? jsx(EmptyState, { title: 'No models downloaded', description: 'Download a model to use it as your default.' })
          : data.models.map(model => jsxs('div', { className: `${row} border-b border-(--ui-stroke-secondary)`, children: [
            jsxs('div', { className: 'grid min-w-0 flex-1 gap-1', children: [
              jsx('span', { className: 'text-sm break-words', children: model.id }),
              jsxs('div', { className: 'flex flex-wrap items-center gap-2', children: [
                jsx('span', { className: `${secondary} tabular-nums`, children: bytes(model.size_bytes) }),
                model.active && jsx(Badge, { variant: 'muted', children: 'Default model' })
              ] })
            ] }),
            jsxs('div', { className: 'flex items-center gap-2', children: [
              jsx(Button, { variant: 'secondary', size: 'sm', disabled: busy || model.active || !data.installed,
                onClick: () => run('/activate', { model_id: model.id }), 'aria-label': `Use ${model.id} as default model`, children: 'Use' }),
              jsx(Button, { variant: 'ghost', size: 'sm', disabled: busy || model.active,
                onClick: () => setDeleting(model.id), 'aria-label': `Delete ${model.id}`, children: 'Delete' })
            ] })
          ] }, model.id)),
        data.model_dir && jsx('p', { className: `${secondary} mt-3 break-words`, children: data.model_dir })
      ] })
    ] }),
    jsx(ConfirmDialog, {
      open: deleting !== null, onClose: () => setDeleting(null), destructive: true,
      title: 'Delete model?', description: `Remove ${deleting || 'this model'} from this host? The downloaded files will be deleted.`,
      confirmLabel: 'Delete', busyLabel: 'Deleting…', doneLabel: 'Deleted',
      onConfirm: async () => {
        if (busy || unavailable || !deleting) throw new Error('The host is busy or unavailable. Refresh and try again.')
        await action.mutateAsync({ path: '/delete', body: { model_id: deleting } })
      }
    })
  ] })
}

function Settings({ ctx }) {
  const connectionId = useValue(host.state.connectionId)
  const profile = useValue(host.state.profile)
  const epoch = useGatewayActivationEpoch()
  return jsx(ScopedSettings, { ctx, connectionId, profile, epoch }, JSON.stringify([connectionId, profile, epoch]))
}

export default {
  id: 'omlx',
  name: 'oMLX',
  register(ctx) {
    ctx.registerSettingsPage({ id: 'settings', title: 'oMLX', icon: 'server', render: () => jsx(Settings, { ctx }) })
  }
}
