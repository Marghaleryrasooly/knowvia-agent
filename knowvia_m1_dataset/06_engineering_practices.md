# NovaTech Engineering Practices

## Code Review
All production code changes require review and approval from at least one other engineer before merging.

## Main Branch Protection
Direct pushes to the main branch are prohibited. Changes must be merged through an approved pull request.

## Automated Tests
Critical application logic must have automated tests. Pull requests should not be merged when required CI checks are failing.

## Secrets
API keys, passwords, access tokens, and private credentials must never be committed to source control.

## Production Deployments
Production deployments require all mandatory CI checks to pass. Emergency changes must be documented after deployment.
