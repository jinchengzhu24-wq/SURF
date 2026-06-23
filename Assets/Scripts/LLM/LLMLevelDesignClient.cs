using System;
using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Networking;

public class LLMLevelDesignClient : MonoBehaviour
{
    public string endpoint = "http://127.0.0.1:8000/generate-level-plan";

    public IEnumerator RequestPlan(Action<LevelDesignPlan> onSuccess)
    {
        using (UnityWebRequest request = UnityWebRequest.Get(endpoint))
        {
            yield return request.SendWebRequest();

            if (request.result != UnityWebRequest.Result.Success)
            {
                Debug.LogWarning("LLMLevelDesignClient failed: " + request.error);
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

            onSuccess?.Invoke(plan);
        }
    }
}
