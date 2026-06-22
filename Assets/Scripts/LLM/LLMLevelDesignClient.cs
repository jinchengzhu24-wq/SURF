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

            LevelDesignPlan plan = JsonUtility.FromJson<LevelDesignPlan>(request.downloadHandler.text);
            onSuccess?.Invoke(plan);
        }
    }
}
