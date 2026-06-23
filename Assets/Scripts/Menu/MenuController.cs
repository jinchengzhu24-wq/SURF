using UnityEngine;
using UnityEngine.SceneManagement;

public class MenuController : MonoBehaviour
{
    public string targetSceneName = "Level_3(H)";

    public void StartGame()
    {
        if (string.IsNullOrEmpty(targetSceneName))
        {
            Debug.LogWarning("MenuController: Target scene name is empty.");
            return;
        }

        SceneManager.LoadScene(targetSceneName);
    }
}
