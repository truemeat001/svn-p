// Entire fixture is fictional. No repository connection, history or real file reads.
const r=(path,item,changelist='',extra={})=>({id:path,path,item,changelist,props:'none',revision:2841,last_changed_author:'chen',kind:'file',...extra});
export const demoRows=[
 r('Assets/Scripts/Player/PlayerController.cs','modified','角色移动与交互'),
 r('Assets/Scripts/Player/InteractionSystem.cs','modified','角色移动与交互'),
 r('Assets/Scripts/Player/CameraRig.cs','modified','角色移动与交互'),
 r('Assets/Prefabs/Player.prefab','modified','角色移动与交互'),
 r('Assets/Tests/PlayerMovementTests.cs','added','角色移动与交互',{revision:null}),
 r('Assets/UI/Theme/Colors.uss','modified','界面细节'),
 r('Assets/UI/InventoryPanel.uxml','modified','界面细节'),
 r('Assets/Textures/UI/inventory-slot.png','added','界面细节',{revision:null}),
 r('Assets/Scenes/Forest.unity','conflicted'),
 r('Assets/Scripts/Legacy/OldInput.cs','deleted'),
 r('Notes/交互设计草案.md','unversioned','',{revision:null}),
 r('Assets/Textures/Reference/moodboard.png','unversioned','',{revision:null}),
];
export const demoDiff=[
 ['h','@@ -24,12 +24,15 @@ public class PlayerController : MonoBehaviour'],
 [' ','    private void Update()'],
 [' ','    {'],
 ['-','        var input = ReadInput();'],
 ['-','        transform.Translate(input * moveSpeed * Time.deltaTime);'],
 ['+','        var input = inputReader.MoveInput;'],
 ['+','        var direction = GetCameraRelativeDirection(input);'],
 ['+','        motor.Move(direction, moveSpeed);'],
 [' ',''],
 ['+','        if (inputReader.InteractPressed)'],
 ['+','            interactionSystem.TryInteract();'],
 [' ','    }'],
 [' ',''],
 [' ','    private Vector3 GetCameraRelativeDirection(Vector2 input)'],
];
export const demoHistory=[
 {revision:2841,author:'chen',time:'今天 10:42',message:'调整角色移动速度与碰撞边界'},
 {revision:2837,author:'lin',time:'昨天 17:26',message:'统一输入读取与相机方向'},
 {revision:2829,author:'chen',time:'10月30日 14:08',message:'加入基础角色交互组件'},
];
