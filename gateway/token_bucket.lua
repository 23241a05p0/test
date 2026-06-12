-- token_bucket.lua
-- Atomic token-bucket refill-and-spend, run entirely inside Redis so that
-- concurrent requests cannot race each other.
--
-- KEYS[1]            = bucket key, e.g. "rl:1.2.3.4"
-- ARGV[1] = rate     = tokens added per second (refill rate)
-- ARGV[2] = capacity = max tokens the bucket can hold (burst size)
-- ARGV[3] = now      = current time in seconds (float), passed from the app
-- ARGV[4] = cost     = how many tokens this request spends (usually 1)
--
-- Returns 1 if the request is ALLOWED (a token was spent),
--         0 if DENIED (not enough tokens).

local key      = KEYS[1]
local rate     = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])
local cost     = tonumber(ARGV[4])

-- Read the existing bucket: current token count and the last refill time.
local bucket = redis.call("HMGET", key, "tokens", "ts")
local tokens = tonumber(bucket[1])
local ts     = tonumber(bucket[2])

-- First time we see this client: start with a full bucket.
if tokens == nil then
  tokens = capacity
  ts     = now
end

-- Refill: add tokens for the time elapsed since we last saw this client,
-- but never exceed the capacity.
local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * rate)
ts     = now

local allowed = 0
if tokens >= cost then
  tokens  = tokens - cost
  allowed = 1
end

-- Persist the new state and let idle buckets expire so Redis stays clean.
-- TTL = time to fully refill an empty bucket, plus a small buffer.
local ttl = math.ceil(capacity / rate) + 1
redis.call("HSET", key, "tokens", tokens, "ts", ts)
redis.call("EXPIRE", key, ttl)

return allowed
