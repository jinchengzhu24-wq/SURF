using UnityEngine;
using UnityEngine.SceneManagement;

public class PartialCompletionIntroController : MonoBehaviour
{
    [Header("Navigation")]
    public string nextSceneName = "PC_Level";

    public void Confirm()
    {
        if (string.IsNullOrWhiteSpace(nextSceneName))
        {
            Debug.LogWarning(
                "PartialCompletionIntroController: nextSceneName is empty.",
                this
            );
            return;
        }

        if (!Application.CanStreamedLevelBeLoaded(nextSceneName))
        {
            Debug.LogError(
                "PartialCompletionIntroController: Scene is not enabled in "
                + "Build Settings: "
                + nextSceneName,
                this
            );
            return;
        }

        SceneManager.LoadScene(nextSceneName);
    }
}
