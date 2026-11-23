// A native Cordis adapter. The Python host owns credentials, billing and tool scope.
export const name = 'eduagents-bridge';
export const inject = ['llm', 'tools'];

export async function apply(ctx) {
  const endpoint = process.env.EDU_DSH_ENDPOINT;
  const token = process.env.EDU_DSH_TOKEN;
  async function post(path, body, signal) {
    const response = await fetch(`${endpoint}/${path}`, {
      method: 'POST', signal,
      headers: { 'content-type': 'application/json', authorization: `Bearer ${token}` },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`Education bridge failed (${response.status})`);
    return response;
  }
  const adapter = {
    providerInfo: id => ({ id, name: 'EduAgents ModelGateway' }),
    providerRetryPolicy: () => undefined,
    imageRequestPricing: () => undefined,
    imageRequestTarget: (_provider, _model, image) => ({ width: image.width, height: image.height, maxImageBytes: image.bytes, imageTargetBytes: image.bytes }),
    listModels: async provider => [{ provider, id: 'education', name: 'Education' }],
    resolveModel: async (provider, id) => ({ provider, id, name: id, context: { contextWindow: 1000000 } }),
    async prepareCall(provider, model) {
      return { model: await this.resolveModel(provider, model), stream: options => this.stream(options) };
    },
    async *stream(options) {
      const response = await post('model', {
        messages: options.messages, tools: options.tools ?? [], purpose: options.purpose,
      }, options.signal);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let pending = '';
      try {
        while (true) {
          const { value, done } = await reader.read();
          pending += decoder.decode(value, { stream: !done });
          let newline;
          while ((newline = pending.indexOf('\n')) >= 0) {
            const line = pending.slice(0, newline);
            pending = pending.slice(newline + 1);
            if (line) yield JSON.parse(line);
          }
          if (done) break;
        }
        if (pending.trim()) throw new Error('Truncated education model stream');
      } finally { await reader.cancel().catch(() => {}); }
    },
  };
  ctx.llm.registerAdapter(['eduagents'], adapter);
  const response = await post('tools', {}, undefined);
  for (const schema of await response.json()) {
    ctx.tools.register({
      ...schema,
      output: {
        schema: { type: 'string' },
        render: (_args, value) => [{ type: 'text', text: value }],
      },
      async execute(args, exec) {
        const response = await post('tool', { name: schema.name, args }, exec.signal);
        return JSON.stringify(await response.json());
      },
    });
  }
}
