export const meta = {
  name: 'integrate-github-investigation',
  description: 'Port legacy GitHub investigation logic into agent.py using async patterns.',
  phases: [
    { title: 'Analyze', detail: 'Read agent.py to identify injection points for async GitHub logic.' },
    { title: 'Implement', detail: 'Update agent.py to use httpx.AsyncClient and asyncio.gather for GitHub API calls.' },
    { title: 'Verify', detail: 'Verify changes with test_production_hardening.py.' },
  ],
}

phase('Analyze')
const agentCode = await agent('Read agent.py and explain how to replace the current thread-based GitHub investigation with an async implementation using httpx.AsyncClient. Highlight specific methods that need updating.', {
  phase: 'Analyze',
  model: 'claude-sonnet-5-5'
})
log('Analysis completed: ' + agentCode)

phase('Implement')
const implementation = await agent('Rewrite the identified sections in agent.py to use httpx.AsyncClient for GitHub API calls instead of requests and ThreadPoolExecutor. Ensure 100% async performance and safety.', {
  phase: 'Implement',
  model: 'claude-sonnet-5-5'
})
log('Implementation code generated.')

// We do not actually apply the changes here as the user has not authorized it.
// The workflow is just a tool to plan and generate the code.
// I will output the plan in the final response.
