using System.Text;
using UnityEngine;
using UnityEngine.Networking;

public static class UnityJsonWebRequest
{
    public static UnityWebRequest CreatePost(
        string url,
        string jsonBody,
        int timeoutSeconds,
        string bearerToken = null)
    {
        UnityWebRequest request = new UnityWebRequest(url, UnityWebRequest.kHttpVerbPOST);
        byte[] body = Encoding.UTF8.GetBytes(jsonBody ?? "{}");
        request.uploadHandler = new UploadHandlerRaw(body);
        request.downloadHandler = new DownloadHandlerBuffer();
        request.SetRequestHeader("Content-Type", "application/json");
        request.SetRequestHeader("Accept", "application/json");
        request.timeout = Mathf.Max(1, timeoutSeconds);

        if (!string.IsNullOrWhiteSpace(bearerToken))
        {
            request.SetRequestHeader("Authorization", "Bearer " + bearerToken);
        }

        return request;
    }

    public static UnityWebRequest CreateGet(
        string url,
        int timeoutSeconds,
        string bearerToken = null)
    {
        UnityWebRequest request = UnityWebRequest.Get(url);
        request.SetRequestHeader("Accept", "application/json");
        request.timeout = Mathf.Max(1, timeoutSeconds);

        if (!string.IsNullOrWhiteSpace(bearerToken))
        {
            request.SetRequestHeader("Authorization", "Bearer " + bearerToken);
        }

        return request;
    }
}
