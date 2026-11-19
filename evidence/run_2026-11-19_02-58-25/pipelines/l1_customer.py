from pyspark.sql import SparkSession, functions as F
from pyspark.sql.types import StructType, StructField, StringType, TimestampType

# Define the schema for the target table
customer_schema = StructType([
    StructField("customer_id", StringType(), False),
    StructField("first_name", StringType(), False),
    StructField("last_name", StringType(), False),
    StructField("email", StringType(), True),
    StructField("phone", StringType(), True),
    StructField("city", StringType(), True),
    StructField("state_province", StringType(), True),
    StructField("country_code", StringType(), True),
    StructField("created_date", TimestampType(), False),
    StructField("modified_date", TimestampType(), False),
    StructField("source_file", StringType(), False),
    StructField("ingestion_timestamp", TimestampType(), False),
    StructField("batch_id", StringType(), False)
])

def build_customer_l1(df):
    # Cast CustomerID to string
    df = df.withColumn("customer_id", F.col("CustomerID").cast("string"))
    
    # Direct transformations
    df = df.withColumn("first_name", F.col("FirstName"))
    df = df.withColumn("last_name", F.col("LastName"))
    df = df.withColumn("email", F.col("Email"))
    df = df.withColumn("phone", F.col("Phone"))
    df = df.withColumn("city", F.col("City"))
    df = df.withColumn("state_province", F.col("StateProvince"))
    df = df.withColumn("country_code", F.col("CountryCode").cast("string"))
    df = df.withColumn("created_date", F.col("CreatedDate"))
    df = df.withColumn("modified_date", F.col("ModifiedDate"))
    df = df.withColumn("source_file", F.col("source_file"))
    df = df.withColumn("ingestion_timestamp", F.current_timestamp())
    df = df.withColumn("batch_id", F.input_file_name())
    
    return df

def load():
    # Read from ADLS Gen2
    df = spark.read.format("delta").load("abfss://landing@adlsdeaccel.dfs.core.windows.net/sqlserver/sales/customer/")
    
    # Transform the data
    df = build_customer_l1(df)
    
    # Write to Delta table
    df.write.format("delta").saveAsTable("dea_accelerator.bronze.customer")

# Create a SparkSession
spark = SparkSession.builder.getOrCreate()

# Load the data
load()