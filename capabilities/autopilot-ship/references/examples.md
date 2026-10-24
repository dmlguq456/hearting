# Examples

### Example 1 — Initial Vercel setup

```
/autopilot-ship "가사관리앱 배포 셋업"
→ Found spec/가사관리/ and no ship.md → initial ship setup
→ Detected Next.js + Prisma + Turso stack → recommend Vercel
→ List keys in .env.example (DATABASE_URL / NEXT_PUBLIC_X / ...)
→ Create optional vercel.json + GitHub Actions deploy.yml
→ Provide deployment commands: vercel login / link / deploy --prod
→ Write spec/가사관리/ship.md
```

### Example 2 — Environment-variable update (repeat invocation)

```
/autopilot-ship "STRIPE_KEY 환경 변수 추가"
→ ship.md exists → repeat-invocation path
→ Add STRIPE_KEY to .env.example
→ Update the env-vars section in ship.md
→ Tell the user to enter the real value in the Vercel dashboard
```

### Example 3 — Domain connection (repeat invocation)

```
/autopilot-ship "homemanager.app 도메인 연결"
→ Explain the DNS A record / CNAME
→ Explain how to add the domain in the Vercel dashboard
→ Update the Domain section in ship.md
```
