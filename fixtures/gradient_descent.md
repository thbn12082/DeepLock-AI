# Gradient Descent căn bản

## Gradient và hướng cập nhật

Gradient của hàm mất mát tại một điểm cho biết hướng tăng nhanh nhất của hàm.
Vì muốn giảm loss, gradient descent cập nhật tham số theo hướng ngược gradient.
Với trọng số `w`, learning rate `η` và loss `L`, một bước cập nhật là:

```text
w_(t+1) = w_t - η ∇L(w_t)
```

Ví dụ, nếu `w=2`, gradient bằng `3` và learning rate bằng `0.1`, trọng số mới
là `2 - 0.1 × 3 = 1.7`. Dấu trừ là quan trọng: cộng gradient thường đi theo
hướng làm loss tăng trong bài toán minimization.

## Learning rate

Learning rate điều khiển độ dài của mỗi bước cập nhật. Learning rate quá nhỏ
làm quá trình học chậm và cần nhiều bước. Learning rate vừa phải có thể giúp
loss giảm ổn định. Learning rate quá lớn có thể khiến cập nhật vượt qua vùng
cực tiểu, làm loss dao động hoặc phân kỳ. Vì vậy learning rate lớn không đồng
nghĩa mô hình luôn học nhanh hơn một cách ổn định.

## Đọc một bước tối ưu

Một bước gradient descent có thể đọc như chuỗi sau:

```text
trọng số hiện tại + gradient + learning rate
    -> nhân gradient với learning rate
    -> trừ độ dịch chuyển khỏi trọng số
    -> trọng số mới
```

Nếu gradient bằng `0`, công thức không thay đổi trọng số ở bước đó. Điều này có
thể xảy ra tại điểm dừng, nhưng một gradient bằng không không tự chứng minh đó là
cực tiểu toàn cục. Một lỗi phổ biến khác là nhầm gradient với loss: loss là giá
trị mục tiêu, còn gradient mô tả độ dốc của loss theo tham số.

## Batch update

Trong mini-batch gradient descent, gradient của một batch được dùng để thực hiện
một lần cập nhật. Batch lớn hơn thường cho ước lượng gradient ít nhiễu hơn nhưng
cần nhiều bộ nhớ hơn. Batch nhỏ hơn tạo nhiều lần cập nhật và gradient có thể
nhiễu hơn. Tài liệu này không khẳng định một batch size duy nhất là tốt nhất cho
mọi bài toán.

