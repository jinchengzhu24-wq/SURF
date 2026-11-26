using System;
using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public class LLMLevelPlanCache : MonoBehaviour
{
    public static LLMLevelPlanCache Instance { get; private set; }

    [Header("References")]
    public LLMLevelDesignClient llmClient;

    [Header("Cache")]
    public int targetCachedPlanCount = 3;
    public int maxTotalPlanRequests = 3;
    public bool prefetchOnStart = false;
    public bool logCacheEvents = true;

    private readonly Queue<LevelDesignPlan> cachedPlans = new Queue<LevelDesignPlan>();
    private bool isRequesting;
    private int completedPlanRequests;

    public int CachedPlanCount
    {
        get { return cachedPlans.Count; }
    }

    public bool IsRequesting
    {
        get { return isRequesting; }
    }

    private void Awake()
    {
        if (Instance != null && Instance != this)
        {
            Destroy(gameObject);
            return;
        }

        Instance = this;
        prefetchOnStart = false;

        // Decommission cross-scene persistence and idle background prefetch when
        // no active consumer exists or the scene uses pure algorithmic generation.
        if (!HasActivePlanConsumer())
        {
            enabled = false;
            return;
        }

        ResolveClient();
    }

    private void Start()
    {
        // Background prefetch on Start is decommissioned so scenes such as
        // Algorithm_Level never fire unconsumed /generate-level-plan requests.
        prefetchOnStart = false;
    }

    private void OnDisable()
    {
        CancelPrefetch();
    }

    private void OnDestroy()
    {
        if (Instance == this)
        {
            Instance = null;
        }

        CancelPrefetch();
    }

    private void CancelPrefetch()
    {
        StopAllCoroutines();
        bool wasRequesting = isRequesting;
        isRequesting = false;

        if (wasRequesting && llmClient != null)
        {
            llmClient.CancelActiveRequests();
        }
    }

    public bool TryTakePlan(out LevelDesignPlan plan)
    {
        if (cachedPlans.Count == 0)
        {
            plan = null;
            return false;
        }

        plan = cachedPlans.Dequeue();

        if (logCacheEvents)
        {
            Debug.Log(
                "LLMLevelPlanCache using cached plan:"
                + " remaining=" + cachedPlans.Count
                + ", completedRequests=" + completedPlanRequests
                + ", maxTotalPlanRequests=" + maxTotalPlanRequests
            );
        }

        EnsurePlanBuffer();
        return true;
    }

    public void EnsurePlanBuffer()
    {
        // Unconsumed automatic background prefetch is decommissioned.
        isRequesting = false;
    }

    private static bool HasActivePlanConsumer()
    {
        LevelLoader loader = FindObjectOfType<LevelLoader>();
        if (loader != null && !loader.useLLMPlan)
        {
            return false;
        }

        return false;
    }

    public IEnumerator WaitForPlan(float timeoutSeconds, Action<LevelDesignPlan> onComplete)
    {
        float timeoutAt = Time.realtimeSinceStartup + Mathf.Max(0f, timeoutSeconds);

        while (cachedPlans.Count == 0 && isRequesting && Time.realtimeSinceStartup < timeoutAt)
        {
            yield return null;
        }

        if (TryTakePlan(out LevelDesignPlan plan))
        {
            onComplete?.Invoke(plan);
            yield break;
        }

        if (logCacheEvents)
        {
            Debug.Log(
                "LLMLevelPlanCache cache miss:"
                + " cached=" + cachedPlans.Count
                + ", isRequesting=" + isRequesting
            );
        }

        onComplete?.Invoke(null);
    }

    private IEnumerator FillBufferRoutine()
    {
        ResolveClient();

        if (llmClient == null)
        {
            Debug.LogWarning("LLMLevelPlanCache: Cannot prefetch because LLMLevelDesignClient is missing.");
            yield break;
        }

        isRequesting = true;

        while (CanRequestMorePlans())
        {
            if (logCacheEvents)
            {
                Debug.Log(
                    "LLMLevelPlanCache prefetch started:"
                    + " cached=" + cachedPlans.Count
                    + ", target=" + targetCachedPlanCount
                    + ", completedRequests=" + completedPlanRequests
                    + ", maxTotalPlanRequests=" + maxTotalPlanRequests
                );
            }

            LevelDesignPlan plan = null;
            yield return llmClient.RequestPlan(result => plan = result);

            if (plan == null)
            {
                Debug.LogWarning(
                    "LLMLevelPlanCache: Prefetch failed."
                    + " cached=" + cachedPlans.Count
                    + ", completedRequests=" + completedPlanRequests
                );
                break;
            }

            cachedPlans.Enqueue(plan);
            completedPlanRequests++;

            if (logCacheEvents)
            {
                Debug.Log(
                    "LLMLevelPlanCache cached plan ready:"
                    + " cached=" + cachedPlans.Count
                    + ", target=" + targetCachedPlanCount
                    + ", completedRequests=" + completedPlanRequests
                    + ", maxTotalPlanRequests=" + maxTotalPlanRequests
                );
            }
        }

        isRequesting = false;
    }

    private bool CanRequestMorePlans()
    {
        int targetCount = Mathf.Max(0, targetCachedPlanCount);
        int totalLimit = Mathf.Max(0, maxTotalPlanRequests);

        if (cachedPlans.Count >= targetCount)
        {
            return false;
        }

        if (totalLimit > 0 && completedPlanRequests >= totalLimit)
        {
            return false;
        }

        return true;
    }

    private void ResolveClient()
    {
        if (llmClient == null)
        {
            llmClient = GetComponent<LLMLevelDesignClient>();
        }

        if (llmClient == null)
        {
            llmClient = FindObjectOfType<LLMLevelDesignClient>();
        }
    }
}
