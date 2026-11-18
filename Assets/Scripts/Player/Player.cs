using UnityEngine;

public class Player : GridPlayerController
{
    protected override Vector3 ReadMoveDirection()
    {
        Vector3 dir = Vector3.zero;

        if (Input.GetKeyDown(KeyCode.W))
        {
            dir = Vector3.up;
        }

        if (Input.GetKeyDown(KeyCode.S))
        {
            dir = Vector3.down;
        }

        if (Input.GetKeyDown(KeyCode.A))
        {
            dir = Vector3.left;
        }

        if (Input.GetKeyDown(KeyCode.D))
        {
            dir = Vector3.right;
        }

        return dir;
    }
}
