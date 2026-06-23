using System;
using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Networking;

#if UNITY_EDITOR
using UnityEditor;
#endif

public class LLMLevelDesignClient : MonoBehaviour
{
    public string endpoint = "http://127.0.0.1:8000/generate-level-plan";
    public int requestTimeoutSeconds = 20;

    private readonly List<UnityWebRequest> activeRequests = new List<UnityWebRequest>();
    private bool isCancellingRequests;

#if UNITY_EDITOR
    [InitializeOnLoadMethod]
    private static void RegisterAssemblyReloadHook()
    {
        AssemblyReloadEvents.beforeAssemblyReload -= CancelAllActiveClients;
        AssemblyReloadEvents.beforeAssemblyReload += CancelAllActiveClients;
    }

    private static void CancelAllActiveClients()
    {
        LLMLevelDesignClient[] clients = Resources.FindObjectsOfTypeAll<LLMLevelDesignClient>();

        for (int i = 0; i < clients.Length; i++)
        {
            clients[i].CancelActiveRequests();
        }
    }
#endif

    private void OnEnable()
    {
        isCancellingRequests = false;
    }

    private void OnDisable()
    {
        CancelActiveRequests();
    }

    private void OnDestroy()
    {
        CancelActiveRequests();
    }

    public void CancelActiveRequests()
    {
        isCancellingRequests = true;

        for (int i = activeRequests.Count - 1; i >= 0; i--)
        {
            UnityWebRequest request = activeRequests[i];

            if (request == null)
            {
                continue;
            }

            request.Abort();
            request.Dispose();
            activeRequests.RemoveAt(i);
        }
    }

    public IEnumerator RequestPlan(Action<LevelDesignPlan> onSuccess)
    {
        if (!isActiveAndEnabled)
        {
            onSuccess?.Invoke(null);
            yield break;
        }

        isCancellingRequests = false;

        UnityWebRequest request = UnityWebRequest.Get(endpoint);
        request.timeout = Mathf.Max(1, requestTimeoutSeconds);
        activeRequests.Add(request);

        UnityWebRequestAsyncOperation operation = request.SendWebRequest();

        while (!operation.isDone)
        {
            if (isCancellingRequests || !isActiveAndEnabled)
            {
                request.Abort();
                CleanupRequest(request);
                onSuccess?.Invoke(null);
                yield break;
            }

            yield return null;
        }

        if (request.result != UnityWebRequest.Result.Success)
        {
            if (!isCancellingRequests)
            {
                Debug.LogWarning("LLMLevelDesignClient failed: " + request.error);
            }

            CleanupRequest(request);
            onSuccess?.Invoke(null);
            yield break;
        }

        LevelDesignPlan plan = null;

        try
        {
            plan = JsonUtility.FromJson<LevelDesignPlan>(request.downloadHandler.text);
        }
        catch (Exception exception)
        {
            Debug.LogWarning("LLMLevelDesignClient could not parse plan JSON: " + exception.Message);
        }

        if (plan != null)
        {
            Debug.Log(
                "LLMLevelDesignClient received plan:"
                + " solutionSteps=" + plan.minSolutionSteps + "-" + plan.maxSolutionSteps
                + ", pushes=" + plan.minPushes + "-" + plan.maxPushes
                + ", waterAreas=" + plan.minWaterAreas + "-" + plan.maxWaterAreas
                + ", wallObstacleBlocks=" + plan.minWallObstacleBlocks + "-" + plan.maxWallObstacleBlocks
                + ", reversePulls=" + plan.minReversePulls + "-" + plan.maxReversePulls
                + ", archetype=" + plan.archetype
                + ", targetLayout=" + plan.targetLayout
                + ", obstacleStyle=" + plan.obstacleStyle
                + ", waterStyle=" + plan.waterStyle
                + ", style=" + plan.style
            );
        }

        CleanupRequest(request);
        onSuccess?.Invoke(plan);
    }

    private void CleanupRequest(UnityWebRequest request)
    {
        if (request == null)
        {
            return;
        }

        if (activeRequests.Remove(request))
        {
            request.Dispose();
        }
    }
}
