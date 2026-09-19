variable "ami_id" {
  description = "AMI ID baked from the configured instance (aws ec2 create-image)"
  type        = string
}

resource "aws_security_group" "alb_sg" {
  name        = "aiops-alb-sg"
  description = "Allow HTTP from the internet to the ALB"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "aiops-alb-sg" }
}

resource "aws_lb_target_group" "app" {
  name        = "aiops-app-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "instance"

  health_check {
    path                = "/health"
    port                = "8000"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
    timeout             = 5
  }

  tags = { Name = "aiops-app-tg" }
}

resource "aws_lb" "app" {
  name               = "aiops-app-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb_sg.id]
  subnets            = [aws_subnet.public.id, aws_subnet.public2.id]

  tags = { Name = "aiops-app-alb" }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

resource "aws_launch_template" "app" {
  name_prefix            = "aiops-app-"
  image_id                = var.ami_id
  instance_type            = var.instance_type
  key_name                 = var.key_name
  vpc_security_group_ids   = [aws_security_group.app_sg.id]

  iam_instance_profile {
    name = aws_iam_instance_profile.app.name
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "aiops-app-asg" }
  }
}

resource "aws_autoscaling_group" "app" {
  name                = "aiops-app-asg"
  min_size            = 1
  max_size            = 3
  desired_capacity    = 1
  vpc_zone_identifier = [aws_subnet.public.id, aws_subnet.public2.id]
  target_group_arns   = [aws_lb_target_group.app.arn]
  health_check_type   = "ELB"
  health_check_grace_period = 60

  launch_template {
    id      = aws_launch_template.app.id
    version = "$Latest"
  }

  tag {
    key                 = "Name"
    value               = "aiops-app-asg"
    propagate_at_launch = true
  }
}